"""In-process session retention and turn admission. Locks protect state, never model execution.

One process retains at most two sessions, one per tenant. One whole turn executes at a time
(the execution permit). At most one more turn waits in memory, and a session has at most one
active or queued turn. Capacity for a running turn is released only when its task finishes.
"""

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass, field

from .errors import Conflict, Denied


@dataclass(eq=False)
class Ticket:
    """One admitted turn. It either holds the execution permit or waits for it."""

    key: str
    enqueued: float
    granted: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def waiting(self):
        return not self.granted.is_set()


@dataclass(eq=False)
class Session:
    tenant: str
    touched: float
    reserved_until: float
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    active: bool = False
    closing: bool = False
    turns: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    fingerprints: dict = field(default_factory=dict)
    ticket: Ticket | None = None
    turn_count: int = 0
    conversation: object = None

    @property
    def busy(self):
        return self.ticket is not None

    @property
    def pending_key(self):
        return self.ticket.key if self.ticket else None


class State:
    def __init__(
        self,
        clock=time.monotonic,
        idle_ttl=300,
        lease=30,
        key_ttl=600,
        max_keys=2048,
        max_sessions=2,
        max_turns=16,
    ):
        self.clock, self.idle_ttl, self.lease = clock, idle_ttl, lease
        self.key_ttl, self.max_keys = key_ttl, max_keys
        self.max_sessions, self.max_turns = max_sessions, max_turns
        self.lock = threading.RLock()
        self.sessions, self.requests = {}, {}
        self.drain = False
        self.executing: Ticket | None = None
        self.queued: Ticket | None = None

    def _clean(self):
        now = self.clock()
        # Sessions with an executing or queued turn never expire into a capacity release.
        self.sessions = {
            k: s
            for k, s in self.sessions.items()
            if s.busy
            or (
                not s.closing
                and now < (s.touched + self.idle_ttl if s.active else s.reserved_until)
            )
        }
        self.requests = {
            k: v for k, v in self.requests.items() if v[0] in self.sessions or v[1] > now
        }

    def reserve(self, tenant, key):
        with self.lock:
            self._clean()
            prior = self.requests.get((tenant, key))
            if prior:
                if prior[0] in self.sessions:
                    return self.sessions[prior[0]]
                raise Conflict("reservation_expired_use_new_key")
            if self.drain:
                raise Conflict("draining")
            if any(s.tenant == tenant for s in self.sessions.values()):
                raise Conflict("tenant_session_active")
            if len(self.sessions) >= self.max_sessions:
                raise Conflict("capacity_full")
            if len(self.requests) >= self.max_keys:
                raise Conflict("retry_history_full_wait_for_expiry")
            now = self.clock()
            session = Session(tenant, now, now + self.lease)
            self.sessions[session.id] = session
            self.requests[(tenant, key)] = (session.id, now + self.key_ttl)
            return session

    def authorize(self, sid, token):
        with self.lock:
            self._clean()
            session = self.sessions.get(sid)
            if (
                not session
                or session.closing
                or not isinstance(token, str)
                or not token.isascii()
                or not secrets.compare_digest(session.token.encode(), token.encode())
            ):
                raise Denied("invalid_session")
            return session

    def begin(self, sid, token, key, fingerprint=""):
        """Admit one turn. A key binds to one payload; completed retries never execute again.

        Returns (session, cached_result). A new turn is in `session.ticket`: it holds the
        permit when `ticket.waiting` is false, otherwise it waits for the running turn.
        """
        with self.lock:
            s = self.authorize(sid, token)
            if key in s.fingerprints and s.fingerprints[key] != fingerprint:
                raise Conflict("idempotency_payload_mismatch")
            if key in s.turns:
                s.touched = self.clock()
                return s, s.turns[key]
            if s.ticket is not None:
                # Also covers a retry of the running key: no second queue position.
                raise Conflict("turn_in_progress")
            if s.turn_count >= self.max_turns:
                raise Conflict("turn_limit_start_new_session")
            if self.executing is not None and self.queued is not None:
                raise Conflict("queue_full")
            ticket = Ticket(key, self.clock())
            if self.executing is None:
                self.executing = ticket
                ticket.granted.set()
            else:
                self.queued = ticket
            s.fingerprints[key] = fingerprint
            s.ticket = ticket
            s.active = True
            s.touched = self.clock()
            return s, None

    def finish(self, s, result):
        """Record a terminal result and release only what this turn held."""
        with self.lock:
            ticket = s.ticket
            if result is not None and ticket is not None:
                s.turns[ticket.key] = result
                s.results[result["turn_id"]] = result
                s.turn_count += 1
                # Conversation memory changes once, together with the cached result, and only
                # for a successful turn. Retries return this result and never commit again.
                commit = result.get("commit")
                if result.get("status") == "complete" and commit and s.conversation is not None:
                    s.conversation.commit(commit)
            if ticket is not None:
                if self.executing is ticket:
                    self.executing = None
                    if self.queued is not None:
                        self.executing, self.queued = self.queued, None
                        self.executing.granted.set()
                elif self.queued is ticket:
                    # A waiting turn that was cancelled or timed out never reached a model.
                    self.queued = None
            s.ticket, s.touched = None, self.clock()
            if s.closing:
                self.sessions.pop(s.id, None)
                s.turns.clear()
                s.results.clear()
                s.conversation = None

    def end(self, sid, token):
        """Close a session. Returns True while its turn still runs or waits."""
        with self.lock:
            s = self.authorize(sid, token)
            s.closing = True
            if not s.busy:
                self.sessions.pop(sid)
                s.turns.clear()
                s.results.clear()
                s.conversation = None
            return s.busy

    def snapshot(self):
        """Occupancy for operator readiness. Retained sessions, not backend health."""
        with self.lock:
            self._clean()
            tenants: dict[str, int] = {}
            for s in self.sessions.values():
                tenants[s.tenant] = tenants.get(s.tenant, 0) + 1
            return {
                "retained_sessions": {
                    "used": len(self.sessions),
                    "max": self.max_sessions,
                    "per_tenant": {
                        t: {"used": tenants.get(t, 0), "max": 1}
                        for t in ("retailer-a", "retailer-b")
                    },
                },
                "executing": int(self.executing is not None),
                "queued": int(self.queued is not None),
                "drain": self.drain,
            }
