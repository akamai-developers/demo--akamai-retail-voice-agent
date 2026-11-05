"""Per-session conversation memory, held in gateway memory only.

State changes once per turn, in `commit`, and only after a successful validated turn. A
failed, cancelled or retried turn never changes the history, the preferences or what
"the second one" means.

Token budgets use an estimate: one token per 3.5 ASCII characters of message text plus two
tokens per other character (accented, non-Latin and emoji text tokenizes far more densely),
rounded up, plus 12 tokens per message for the chat template. The tool schemas are counted as a fixed
1,100 tokens, because the model's chat template renders them far larger than their JSON.
These are conservative estimates, not tokenizer counts. vLLM enforces the actual
context limit and rejects oversized requests with a 400.
"""

import json
import math
import re
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 3.5
REPLY_TOKENS = 512
TOOL_RESERVE_TOKENS = 1200
TOOL_SCHEMA_TOKENS = 1100
MESSAGE_OVERHEAD_TOKENS = 12
MAX_EXCHANGES = 16

# A positional reference to the cards on screen: "the second one", "the first jacket",
# "option 2". A bare "first" ("my first rain jacket") is not a reference.
NOUNS = (
    "one|ones|item|option|product|card|choice|jacket|shell|anorak|fleece|hoodie|vest|hat|cap|"
    "beanie|glove|gloves|mitts|pair"
)
ORDINAL = re.compile(rf"\b(first|second|third|last|1st|2nd|3rd)\s+(?:{NOUNS})\b", re.I)
NUMBERED = re.compile(r"\b(?:option|number|item|choice)\s+(one|two|three|1|2|3)\b", re.I)
POSITIONS = {
    "first": 0,
    "1st": 0,
    "one": 0,
    "1": 0,
    "second": 1,
    "2nd": 1,
    "two": 1,
    "2": 1,
    "third": 2,
    "3rd": 2,
    "three": 2,
    "3": 2,
}


def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    plain = sum(1 for character in text if ord(character) < 128)
    other = len(text) - plain
    return math.ceil(plain / CHARS_PER_TOKEN + 2 * other) + MESSAGE_OVERHEAD_TOKENS


@dataclass(frozen=True)
class Exchange:
    user: str
    assistant: str

    def messages(self):
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ]

    def tokens(self):
        return estimate_tokens(self.user) + estimate_tokens(self.assistant)


@dataclass(frozen=True)
class Reference:
    """What a positional reference resolved to, or the clarification to ask instead."""

    product_id: str | None = None
    clarify: str | None = None
    positional: bool = False


@dataclass
class Conversation:
    tenant: str
    store_id: str
    exchanges: list[Exchange] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    displayed: tuple[str, ...] = ()
    display_revision: int = 0
    selected_product_id: str | None = None
    selected_variant_id: str | None = None
    pending_clarification: str | None = None
    # Shopper-supplied choices tied to one product, including an incomplete color/size choice.
    selection: dict = field(default_factory=dict)
    pending_action: dict = field(default_factory=dict)
    # Only this session's last successful image. Never exposed in model messages or traces.
    preview: dict = field(default_factory=dict)
    # Line items added with add_to_cart. Memory only; gone with the session.
    cart: list[dict] = field(default_factory=list)

    def history_for_model(self, budget_tokens):
        """Newest whole exchanges that fit the budget. Returns (messages, dropped_count)."""
        kept, used = [], 0
        for exchange in reversed(self.exchanges):
            cost = exchange.tokens()
            if used + cost > budget_tokens:
                break
            kept.append(exchange)
            used += cost
        kept.reverse()
        messages = [m for exchange in kept for m in exchange.messages()]
        return messages, len(self.exchanges) - len(kept)

    def resolve_reference(self, text, client_revision=None, selected_product_id=None):
        """Resolve "the second one" against the committed card order.

        A new positional reference takes priority over a clicked or browser-echoed selection.
        A card click needs no position. A positional reference needs the browser's display revision to match the committed one (when the
        browser sent one) and a card at that position. Otherwise the turn asks to clarify.
        """
        match = ORDINAL.search(text) or NUMBERED.search(text)
        if not match:
            return Reference(product_id=selected_product_id)
        if client_revision is not None and client_revision != self.display_revision:
            return Reference(
                clarify="The products on your screen changed. Which one do you mean?",
                positional=True,
            )
        if not self.displayed:
            return Reference(
                clarify="I have not shown any products yet. What are you looking for?",
                positional=True,
            )
        word = match.group(1).lower()
        index = len(self.displayed) - 1 if word == "last" else POSITIONS[word]
        if index >= len(self.displayed):
            count = len(self.displayed)
            return Reference(
                clarify=f"I showed {count} product{'s' if count != 1 else ''}. "
                "Which one do you mean?",
                positional=True,
            )
        return Reference(product_id=self.displayed[index], positional=True)

    def commit(self, update):
        """Apply one successful turn. Called once, under the admission lock."""
        self.exchanges.append(Exchange(update["user"], update["assistant"]))
        del self.exchanges[:-MAX_EXCHANGES]
        if update.get("preferences") is not None:
            self.preferences = dict(update["preferences"])
        self.displayed = tuple(update["displayed"])
        self.display_revision = update["display_revision"]
        self.selected_product_id = update.get("selected_product_id")
        self.selected_variant_id = update.get("selected_variant_id")
        self.pending_clarification = update.get("clarification")
        self.selection = dict(update.get("selection") or {})
        self.pending_action = dict(update.get("pending_action") or {})
        if "preview" in update:
            self.preview = dict(update.get("preview") or {})
        if "cart" in update:
            self.cart = [dict(item) for item in update["cart"]]
