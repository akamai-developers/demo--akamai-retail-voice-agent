import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from retail_voice.admission import State
from retail_voice.api import create_app, fixture_media
from retail_voice.models.mock import MockBackend
from retail_voice.models.nvidia import NvidiaBackend
from retail_voice.pipeline import run_turn
from retail_voice.settings import GPUSettings

KEYS = {"lifecycle-fixture-a": "retailer-a", "lifecycle-fixture-b": "retailer-b"}
OPERATOR = "lifecycle-operator-key"
BODY = {
    "a": {"sku": "jacket-a", "store_id": "store-a"},
    "b": {"sku": "jacket-b", "store_id": "store-b"},
}


def events_of(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def app(self, backend, **kwargs):
        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            return create_app(KEYS, OPERATOR, backend=backend, **kwargs)

    async def setup_session(self, client, tenant, idem="setup"):
        response = await client.post(
            "/v1/sessions",
            headers={
                "Authorization": f"Bearer lifecycle-fixture-{tenant}",
                "Idempotency-Key": idem,
            },
            json={},
        )
        return response

    def turn(self, client, s, tenant, key):
        return client.post(
            f"/v1/sessions/{s['session_id']}/fixture-turn",
            headers={"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": key},
            json=BODY[tenant],
        )

    async def test_end_cancels_real_api_execution_before_releasing_slot(self):
        started, cleaned = asyncio.Event(), asyncio.Event()

        class Slow(MockBackend):
            async def transcribe(self, audio):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()

        app = self.app(Slow())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            s = (await self.setup_session(client, "a")).json()
            url = f"/v1/sessions/{s['session_id']}"
            headers = {"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": "turn"}
            pending = asyncio.create_task(self.turn(client, s, "a", "turn"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                same_tenant = await self.setup_session(client, "a", "second")
                self.assertEqual(same_tenant.status_code, 409)
                other = await self.setup_session(client, "b", "other")
                self.assertEqual(other.status_code, 200)
                ended = await client.delete(url, headers=headers)
                self.assertEqual(ended.status_code, 200)
                self.assertTrue(cleaned.is_set())
            finally:
                if not pending.done():
                    await client.delete(url, headers=headers)
                response = await pending
            self.assertIn('"reason": "cancelled"', response.text)
            self.assertNotIn("turn.completed", response.text)
            self.assertEqual(
                [x.tenant for x in app.state.admission.sessions.values()], ["retailer-b"]
            )
            self.assertIsNone(app.state.admission.executing)
            accepted = await self.setup_session(client, "a", "third")
            self.assertEqual(accepted.status_code, 200)

    async def test_deadline_failure_can_retry_without_model_reexecution(self):
        class Slow(MockBackend):
            calls = 0

            async def transcribe(self, audio):
                self.calls += 1
                await asyncio.sleep(60)

        backend = Slow()
        app = self.app(backend, deadline=1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            s = (await self.setup_session(client, "a")).json()
            first = await self.turn(client, s, "a", "turn")
            second = await self.turn(client, s, "a", "turn")
            self.assertIn('"reason": "deadline"', first.text)
            self.assertEqual(first.text, second.text)
            self.assertEqual(backend.calls, 1)
            self.assertFalse(app.state.admission.sessions[s["session_id"]].busy)
            self.assertEqual(app.state.admission.sessions[s["session_id"]].turn_count, 1)

    async def test_two_tenants_alternate_with_visible_waiting(self):
        gate, first_started = asyncio.Event(), asyncio.Event()
        observed = []

        class Gated(MockBackend):
            async def transcribe(self, audio):
                state = app.state.admission
                observed.append(sum(1 for x in state.sessions.values() if x.busy))
                if not first_started.is_set():
                    first_started.set()
                    await gate.wait()
                return await super().transcribe(audio)

        app = self.app(Gated())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            a = (await self.setup_session(client, "a")).json()
            b = (await self.setup_session(client, "b")).json()
            ta = asyncio.create_task(self.turn(client, a, "a", "a1"))
            await asyncio.wait_for(first_started.wait(), 2)
            tb = asyncio.create_task(self.turn(client, b, "b", "b1"))
            try:
                for _ in range(200):
                    if app.state.admission.queued is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(app.state.admission.queued)
                busy_b = await self.turn(client, b, "b", "b2")
                self.assertEqual(busy_b.status_code, 409)
                self.assertEqual(busy_b.json()["detail"], "turn_in_progress")
                retry_b = await self.turn(client, b, "b", "b1")
                self.assertEqual(retry_b.status_code, 409)
            finally:
                gate.set()
                ra, rb = await ta, await tb
            ea, eb = events_of(ra), events_of(rb)
            self.assertEqual(ea[-1]["event_type"], "turn.completed")
            self.assertNotIn("turn.waiting", [e["event_type"] for e in ea])
            self.assertEqual([e["event_type"] for e in eb[:2]], ["turn.started", "turn.waiting"])
            waits = [e for e in eb if e["event_type"] == "turn.waiting"]
            self.assertEqual([w["status"] for w in waits], ["waiting", "complete"])
            self.assertGreater(waits[1]["queued_ms"], 0)
            self.assertEqual(eb[-1]["event_type"], "turn.completed")
            self.assertIn("queued_ms", eb[-1])
            # B's ASR ran only after A released the permit: one busy session each time.
            self.assertEqual(observed, [1, 1])
            # Both tenants keep working sessions without signing out.
            again_a = await self.turn(client, a, "a", "a2")
            self.assertEqual(events_of(again_a)[-1]["event_type"], "turn.completed")
            self.assertEqual(app.state.admission.sessions[a["session_id"]].turn_count, 2)
            self.assertEqual(app.state.admission.sessions[b["session_id"]].turn_count, 1)

    async def test_messages_queue_behind_the_other_tenant(self):
        gate, started = asyncio.Event(), asyncio.Event()

        class Gated(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                if not started.is_set():
                    started.set()
                    await gate.wait()
                return await super().decide(messages, tools, tool_choice)

        app = self.app(Gated())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            a = (await self.setup_session(client, "a")).json()
            b = (await self.setup_session(client, "b")).json()

            def post(s, key, text):
                return client.post(
                    f"/v1/sessions/{s['session_id']}/messages",
                    headers={
                        "Authorization": "Bearer " + s["session_token"],
                        "Idempotency-Key": key,
                    },
                    files={"text": (None, text)},
                )

            ta = asyncio.create_task(post(a, "a1", "rain jacket under 150 dollars"))
            await asyncio.wait_for(started.wait(), 2)
            tb = asyncio.create_task(post(b, "b1", "rain jacket under 150 dollars"))
            try:
                for _ in range(200):
                    if app.state.admission.queued is not None:
                        break
                    await asyncio.sleep(0.01)
                busy = await post(a, "a2", "warm hat")
                self.assertEqual(
                    (busy.status_code, busy.json()["detail"]), (409, "turn_in_progress")
                )
            finally:
                gate.set()
                ra, rb = await ta, await tb
            eb = events_of(rb)
            self.assertEqual([e["event_type"] for e in eb[:2]], ["turn.started", "turn.waiting"])
            self.assertEqual(eb[-1]["event_type"], "turn.completed")
            self.assertEqual(events_of(ra)[-1]["event_type"], "turn.completed")
            sessions = app.state.admission.sessions
            self.assertEqual(len(sessions[a["session_id"]].conversation.exchanges), 1)
            self.assertEqual(len(sessions[b["session_id"]].conversation.exchanges), 1)


class QueuedTurnQuarantineTests(unittest.IsolatedAsyncioTestCase):
    """A queued turn never reaches a model, so cancelling or timing it out is always safe."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = GPUSettings(
            site_id="site-a",
            region="us-test",
            model_root=Path(self.temp.name) / "models",
            runtime_dir=Path(self.temp.name) / "runtime",
        )
        self.settings.runtime_dir.mkdir()
        self.gate, self.asr_started = asyncio.Event(), asyncio.Event()
        self.asr_calls = 0
        self.wav = await MockBackend().synthesize("contract audio")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.backend = NvidiaBackend(self.settings, client=self.client)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def handle(self, request):
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "busy": self.asr_started.is_set() and not self.gate.is_set(),
                    "device": "cuda",
                    "profile_digest": self.settings.profile_digest,
                },
            )
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "retail-nemotron"}]})
        if path == "/asr":
            self.asr_calls += 1
            self.asr_started.set()
            await self.gate.wait()
            return httpx.Response(200, json={"text": "Is navy medium in stock?"})
        if path == "/tts":
            return httpx.Response(200, content=self.wav)
        payload = json.loads(request.content)
        if payload["tool_choice"] == "auto":
            selected = json.loads(payload["messages"][1]["content"][0]["text"])
            call = {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup_inventory",
                    "arguments": json.dumps(
                        {"sku": selected["selected_sku"], "store_id": selected["selected_store"]}
                    ),
                },
            }
            message = {"content": None, "tool_calls": [call]}
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]}
            )
        message = {"content": "There are 3 navy medium jackets."}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": message}]}
        )

    async def queued_outcome(self, stop):
        state = State()
        a = state.reserve("retailer-a", "a")
        b = state.reserve("retailer-b", "b")
        sa, _ = state.begin(a.id, a.token, "ka")
        sb, _ = state.begin(b.id, b.token, "kb")
        audio, image = fixture_media()
        running = asyncio.create_task(
            run_turn(self.backend, sa, audio, image, "jacket-a", "store-a", 30, None, sa.ticket)
        )
        await asyncio.wait_for(self.asr_started.wait(), 2)
        # A's ASR is in flight: the marker is A's and the backend stays healthy for admission.
        self.assertTrue(self.settings.quarantine_path.exists())
        self.assertTrue(await self.backend.healthy())
        deadline = 0.3 if stop == "deadline" else 30
        waiting = asyncio.create_task(
            run_turn(
                self.backend, sb, audio, image, "jacket-b", "store-b", deadline, None, sb.ticket
            )
        )
        await asyncio.sleep(0.05)
        if stop == "cancel":
            waiting.cancel()
        rb = await waiting
        state.finish(sb, rb)
        self.assertEqual(rb["reason"], "cancelled" if stop == "cancel" else "deadline")
        self.assertEqual(
            [e["event_type"] for e in rb["events"]][:2], ["turn.started", "turn.waiting"]
        )
        self.assertIsNone(state.queued)
        self.assertIs(state.executing, sa.ticket)
        self.assertTrue(self.settings.quarantine_path.exists())
        self.assertFalse(self.backend.quarantined)
        self.gate.set()
        ra = await running
        state.finish(sa, ra)
        self.assertEqual(ra["status"], "complete")
        self.assertEqual(self.asr_calls, 1)
        self.assertFalse(self.settings.quarantine_path.exists())
        self.assertFalse(self.backend.quarantined)
        self.assertTrue(await self.backend.healthy())
        self.assertIsNone(state.executing)

    async def test_queued_cancel_leaves_the_running_marker_alone(self):
        await self.queued_outcome("cancel")

    async def test_queued_timeout_leaves_the_running_marker_alone(self):
        await self.queued_outcome("deadline")
