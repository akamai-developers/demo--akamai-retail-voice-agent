from dataclasses import dataclass
from typing import Protocol


@dataclass
class Decision:
    """One model decision: a single tool call, an answer, or a clarifying question."""

    answer: str | None = None
    tool: str | None = None
    arguments: dict | None = None
    call_id: str | None = None
    clarification: str | None = None
    usage: dict | None = None  # token counts reported by the model server, when it reports them
    # The model's text carried tool markup or reasoning tags and is withheld. The loop gives
    # the model one corrective retry; nothing from that text is parsed or shown.
    malformed: str | None = None

    @property
    def text(self):
        return self.answer if self.answer is not None else self.clarification


class Backend(Protocol):
    mode: str
    site_id: str
    region: str
    profile_digest: str
    context_tokens: int

    def stage_metadata(self, stage: str) -> dict: ...
    async def ready(self) -> bool: ...
    async def healthy(self) -> bool: ...

    async def transcribe(self, audio: bytes) -> str: ...
    async def decide(
        self, messages: list[dict], tools: list[dict], tool_choice: str = "auto"
    ) -> Decision: ...
    async def synthesize(self, text: str) -> bytes: ...
    async def close(self) -> None: ...
