"""Conservative stock-answer validation against the shopper's catalog target.

This is a bounded English demo resolver, not a general natural-language parser. Exact
catalog names/ids, committed positions, a selected card and explicit variant words provide
constraints. Missing or conflicting constraints require clarification. Model tool arguments
and prose never establish the shopper's target.
"""

import re
from dataclasses import dataclass

from .catalog_repository import SIZE_WORDS

STOCK_LANGUAGE = re.compile(
    r"\b(available|availability|stock|sold out|sell out|have any left|carry|carried|"
    r"pick up|pickup|buy here|have .{0,60} (?:here|at this store|in (?:navy|red|black|medium|size))|"
    r"(?:we|they) have \d+|\d+ (?:left|remaining|on hand))\b",
    re.I,
)
# An explicit stock assertion. On a turn that asked nothing about stock, only this is checked.
STOCK_CLAIM = re.compile(
    r"\b(in stock|out of stock|sold out|\d+ (?:left|remaining|on hand|in stock))\b", re.I
)
COLORS = "navy black gray charcoal red green olive blue yellow orange tan cream brown white purple pink".split()
APPAREL = {"jackets", "rain-shells", "fleeces", "vests"}
# A whole id token: a product id or a variant id. "jacket-a-no-such-variant" and "jacket-a-123"
# are each one unknown token, never "jacket-a". Segments may hold digits.
PRODUCT_ID = re.compile(r"\b(?:jacket-[ab](?:-[a-z0-9]+)*|[ab]-[a-z0-9]+(?:-[a-z0-9]+)+)\b", re.I)
# Speech recognition writes "product ID jacket B" for the typed "product id jacket-b". Only the
# two short ids have a spoken form, and only after "product" or "id", so "a jacket a size
# larger" is not an id.
SPOKEN_ID = re.compile(r"(\b(?:product\s+)?id\s+|\bproduct\s+)jacket[\s-]*([ab])\b", re.I)


def canonical(text):
    """The shopper's words with spoken ids written the typed way."""
    return SPOKEN_ID.sub(lambda m: m.group(1) + "jacket-" + m.group(2).lower(), text)


def named_product_ids(text):
    """Product or variant ids the shopper typed or spoke, lowercased, in order, once each."""
    return list(dict.fromkeys(m.lower() for m in PRODUCT_ID.findall(canonical(text))))


def contains(text, term):
    return re.search(r"(?<![\w'’-])" + re.escape(term) + r"(?![\w'’-])", text, re.I) is not None


def words(text, choices):
    # Prefer complete phrases: 'extra large' must not also select 'large'.
    values = set()
    remaining = text.lower()
    for word in sorted(choices, key=len, reverse=True):
        if contains(remaining, word):
            values.add(choices[word])
            remaining = re.sub(r"\b" + re.escape(word) + r"\b", " ", remaining)
    return values


@dataclass(frozen=True)
class StockTarget:
    product_id: str | None
    variant_id: str | None
    store_id: str | None
    # The color and size words the shopper used, so a clarification can say which of them
    # this product does not come in instead of asking for everything again.
    colors: frozenset = frozenset()
    sizes: frozenset = frozenset()

    def matches(self, fact):
        return bool(self.product_id and self.variant_id and self.store_id) and all(
            fact.get(key) == getattr(self, key) for key in ("product_id", "variant_id", "store_id")
        )


def stock_target(text, catalog, conversation, reference):
    tenant = conversation.tenant
    text = canonical(text)  # typed and spoken ids resolve the same way
    own = [p for p in catalog.products.values() if p.tenant_id == tenant]
    named = {p.product_id for p in own if contains(text, p.name) or contains(text, p.product_id)}
    if not named:
        # A unique leading phrase such as "Downpour Packable" is enough. Generic words
        # such as "rain shell" and fuzzy spellings do not establish a product identity.
        named = {
            p.product_id
            for p in own
            if any(
                contains(text, " ".join(p.name.split()[:n])) for n in range(2, len(p.name.split()))
            )
        }
    if not named:
        # A distinctive catalog name survives common ASR splits such as "Drizzle an Iraq".
        # Require an item-selection cue (or the name at the start), not weather prose such
        # as "a jacket for drizzle". A shared first word remains ambiguous.
        for product in own:
            anchor = product.name.split()[0]
            if len(anchor) < 4:
                continue
            cue = rf"(?:^|\b(?:the|that|this|try|show|see|about|with|add|like|prefer|want|choose|take)\s+){re.escape(anchor)}\b"
            if re.search(cue, text, re.I):
                named.add(product.product_id)
    # An id token names its product; a variant id also fixes the variant. An unknown or
    # foreign token means there is no target at all.
    forced = set()
    for token in set(PRODUCT_ID.findall(text.lower())):
        pair = catalog.variants.get(token)
        if pair is not None and pair[0].tenant_id == tenant:
            named.add(pair[0].product_id)
            forced.add(token)
        elif catalog.owns_product(tenant, token):
            named.add(token)
        else:
            return StockTarget(None, None, None)
    if len(named) > 1 or len(forced) > 1:
        return StockTarget(None, None, None)
    if not named:
        # An unrecognized newly named item must not fall back to the old preview just because
        # the same sentence also says "it". Ask for the item rather than showing the wrong one.
        new_item = re.search(
            r"\b(?:(?:try|show|see)\s+(?:the\s+)?|(?:add|choose|take)\s+the\s+)(\w+)", text, re.I
        )
        if new_item and new_item.group(1).lower() not in {
            "it",
            "this",
            "that",
            "them",
            "me",
            "on",
            "one",
            "item",
            "same",
            "first",
            "second",
            "third",
            *COLORS,
            *SIZE_WORDS,
        }:
            return StockTarget(None, None, None)
    pointed = reference.product_id
    if reference.positional and named and pointed not in named:
        return StockTarget(None, None, None)
    # A retained card is useful for a follow-up, not for an unnamed new kind of product.
    product_words = (
        r"\b(jackets?|raincoats?|shells?|anorak|fleece|vest|hat|cap|beanie|gloves|mitts|parka)\b"
    )
    pointer_words = r"\b(it|this|that|one|ones|they|them)\b"
    if (
        not named
        and not reference.positional
        and re.search(product_words, text, re.I)
        and not re.search(pointer_words, text, re.I)
    ):
        return StockTarget(None, None, None)
    product_id = next(iter(named), None) or pointed or conversation.selected_product_id
    if not product_id or not catalog.owns_product(tenant, product_id):
        return StockTarget(None, None, None)
    variants = catalog.card(tenant, product_id)["variants"]
    color_words = {c: c for c in COLORS} | {"grey": "gray"}
    colors, sizes = words(text, color_words), words(text, SIZE_WORDS)
    choice = conversation.selection
    if not forced and choice.get("product_id") == product_id:
        if not colors:
            colors = set(choice.get("colors", []))
        if not sizes:
            sizes = set(choice.get("sizes", []))
    if not forced and not sizes and choice.get("product_id") != product_id:
        previous = catalog.products.get(choice.get("product_id"))
        current = catalog.products[product_id]
        # A shopper's medium preference can follow a switch between jackets. It cannot
        # migrate into unrelated sizing systems such as hats or gloves, or supply a color.
        if previous and previous.category in APPAREL and current.category in APPAREL:
            sizes = set(choice.get("sizes", []))
    # Reuse a confirmed variant only for this same product. Otherwise the immediately
    # preceding shopper turn can supply the color of the currently selected product.
    prior = next((v for v in variants if v["variant_id"] == conversation.selected_variant_id), None)
    same = product_id == conversation.selected_product_id
    if not forced and not colors and same and prior:
        colors = {prior["color"]}
    if not forced and not sizes and same and prior:
        sizes = {prior["size"]}
    if not forced and not colors and same:
        # "Does the second one come in navy?", "yes, that's the one", "is medium available?":
        # the color may sit two shopper turns back.
        for exchange in reversed(conversation.exchanges[-2:]):
            colors = words(exchange.user, color_words)
            if colors:
                break
    if not forced and not colors and conversation.preferences.get("color"):
        colors = {conversation.preferences["color"]}
    matches = [
        v
        for v in variants
        if (not colors or v["color"] in colors) and (not sizes or v["size"] in sizes)
    ]
    variant = (
        matches[0]["variant_id"]
        if len(matches) == 1 and len(colors) <= 1 and len(sizes) <= 1
        else None
    )
    if forced:
        # The shopper gave the exact variant. Words that contradict it leave no target.
        variant = next(iter(forced))
        variant = variant if any(v["variant_id"] == variant for v in matches) else None
        if variant:
            exact = next(v for v in variants if v["variant_id"] == variant)
            colors = colors or {exact["color"]}
            sizes = sizes or {exact["size"]}
    stores = catalog.tenant_stores(tenant)
    named_stores = {
        s.store_id for s in stores if contains(text, s.name) or contains(text, s.store_id)
    }
    store = next(iter(named_stores)) if len(named_stores) == 1 else conversation.store_id
    location = re.search(r"\b(?:at|from)\s+([^?.!,]+)", text, re.I)
    unknown_location = (
        location
        and not named_stores
        and location.group(1).lower().strip()
        not in {
            "this store",
            "the store",
            "our store",
            "my store",
            "this location",
            "the current store",
        }
    )
    if (
        len(named_stores) > 1
        or unknown_location
        or (re.search(r"\bstore-[a-z0-9-]+\b", text) and not named_stores)
    ):
        store = None
    return StockTarget(product_id, variant, store, frozenset(colors), frozenset(sizes))


def variant_reply(text, target):
    """A short choice such as 'How about medium?' continues the current product question."""
    if not target.product_id:
        return False
    tokens = re.findall(r"[a-z]+", text.lower())
    choices = set(COLORS) | set(" ".join(SIZE_WORDS).split())
    filler = set(
        "how what about the in size please yes let lets s check that one okay ok great perfect sounds good actually well go with choose prefer i d like want it thanks thank you instead".split()
    )
    return bool(tokens and set(tokens) & choices and set(tokens) <= choices | filler)


def color_question(text, target):
    """Catalog color availability does not ask the shopper to choose a stock variant."""
    return bool(
        target.product_id
        and target.colors
        and not target.sizes
        and re.search(r"\b(?:comes? in|have.{0,70}in)\b", text, re.I)
        and not re.search(
            r"\b(?:stock|available|availability|here|store|left|check|cart)\b", text, re.I
        )
    )


def listed(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


GENERIC_CLARIFICATION = (
    "I could not confirm stock for that exact item. "
    "Which product, color, size and store should I check?"
)


def stock_clarification(target, card=None):
    """The gateway's own clarification when a stock claim cannot be confirmed. With the shopper's
    product known it says what the catalog offers and asks only for what is missing: "blue"
    for a jacket sold in yellow and navy gets the colors, not a form to fill in."""
    if card is None or target is None or not target.product_id:
        return GENERIC_CLARIFICATION
    name, variants = card["name"], card["variants"]
    if target.variant_id:
        v = next(v for v in variants if v["variant_id"] == target.variant_id)
        if not target.store_id:
            return f"Which store should I check for the {name} in {v['color']} {v['size']}?"
        return (
            f"I could not confirm stock for the {name} in {v['color']} {v['size']}. "
            "Ask me to check it and I will."
        )
    colors = list(dict.fromkeys(v["color"] for v in variants))
    sizes = card["sizes"]
    asked_colors = [c for c in colors if c in target.colors] + sorted(target.colors - set(colors))
    asked_sizes = [s for s in sizes if s in target.sizes] + sorted(target.sizes - set(sizes))
    unoffered_colors = [c for c in asked_colors if c not in colors]
    unoffered_sizes = [s for s in asked_sizes if s not in sizes]
    color = next((c for c in asked_colors if c in colors), None)
    size = next((s for s in asked_sizes if s in sizes), None)
    if len(asked_colors) > 1 or len(asked_sizes) > 1:
        return f"Which color and size should I check for the {name}?"
    if color and size:
        sizes_for = [v["size"] for v in variants if v["color"] == color]
        return (
            f"The {name} in {color} comes in {listed(sizes_for)}, not {size}. "
            "Which size should I check?"
        )
    if size:
        colors_for = list(dict.fromkeys(v["color"] for v in variants if v["size"] == size))
        if unoffered_colors:
            return (
                f"The {name} doesn't come in {unoffered_colors[0]}. In size {size} it comes in "
                f"{listed(colors_for)}. Which color should I check?"
            )
        return f"Which color should I check in size {size}? It comes in {listed(colors_for)}."
    if color:
        sizes_for = [v["size"] for v in variants if v["color"] == color]
        if unoffered_sizes:
            return (
                f"The {name} doesn't come in size {unoffered_sizes[0]}. In {color} it comes in "
                f"{listed(sizes_for)}. Which size should I check?"
            )
        return f"Which size should I check in {color}? It comes in {listed(sizes_for)}."
    if unoffered_colors or unoffered_sizes:
        what = f"in {unoffered_colors[0]}" if unoffered_colors else f"in size {unoffered_sizes[0]}"
        return (
            f"The {name} doesn't come {what}. It comes in {listed(colors)}, sizes "
            f"{listed(sizes)}. Which color and size should I check?"
        )
    return (
        f"Which color and size should I check? The {name} comes in {listed(colors)}, "
        f"sizes {listed(sizes)}."
    )


def stock_answer(fact):
    label = f"{fact['product_name']} in {fact['color']} {fact['size']}"
    if fact["quantity"]:
        answer = f"{label}: {fact['quantity']} in stock at {fact['store_name']}."
    else:
        answer = f"{label} is out of stock at {fact['store_name']}."
    return answer


def validate_stock_answer(text, answer, records, target, mock=False, clarification=None):
    """Use only an exact-target fact from this turn for both speech and structured stock.

    A mismatch, ambiguous request or unsupported stock assertion produces the gateway's own
    clarification (`stock_clarification`, or the generic question when none is given). Other
    product facts remain the model's responsibility.
    """
    checks = [r for r in records if r[0] == "check_availability" and r[3]]
    prefix = "Fixture answer: " if mock else ""
    unconfirmed = prefix + (clarification or GENERIC_CLARIFICATION)
    if not STOCK_LANGUAGE.search(text):
        # Not a stock question. A budget change or a "no match" reply keeps its words and its
        # search results even when it says "available" or "carry". A lookup the model volunteered
        # is dropped rather than spoken, unless the reply claims stock and that lookup checked
        # the exact shopper-resolved variant and store: then the verified fact stands
        # in for the claim and is shown. A claim with no such check is replaced.
        kept = [r for r in records if r[0] != "check_availability"]
        if STOCK_CLAIM.search(answer):
            backed = [r for r in checks if target is not None and target.matches(r[2])]
            if backed:
                return prefix + stock_answer(backed[-1][2]), kept + backed[-1:], False
            return unconfirmed, kept, True
        return answer, kept, False
    matching = [r for r in checks if target.matches(r[2])]
    # Do not display or commit another product's stock, even if that lookup was authorized.
    kept = [r for r in records if r[0] != "check_availability"] + matching[-1:]
    if matching:
        return prefix + stock_answer(matching[-1][2]), kept, False
    # Also discard stray details/search for this unconfirmed stock turn. Keep the committed
    # cards and product focus instead of selecting the model's unrelated lookup.
    return unconfirmed, [], True
