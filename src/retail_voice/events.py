import uuid


class Trace:
    def __init__(self, session, backend, on_event=None):
        self.on_event = on_event
        self.session, self.mode, self.backend = session, backend.mode, backend
        self.id, self.turn_id = uuid.uuid4().hex, uuid.uuid4().hex
        self.root = uuid.uuid4().hex
        self.events = []

    def emit(self, kind, status="complete", **attrs):
        event = dict(
            schema_version=1,
            event_id=uuid.uuid4().hex,
            event_type=kind,
            session_id=self.session.id,
            trace_id=self.id,
            turn_id=self.turn_id,
            span_id=self.root if not self.events else uuid.uuid4().hex,
            parent_span_id=None if not self.events else self.root,
            source="regional-gateway",
            sequence=len(self.events),
            mode=self.mode,
            status=status,
            tenant=self.session.tenant,
            site_id=self.backend.site_id,
            region=self.backend.region,
            profile_digest=self.backend.profile_digest,
            pipeline="pipecat",
            **attrs,
        )
        self.events.append(event)
        if self.on_event:
            self.on_event(event)
        return event
