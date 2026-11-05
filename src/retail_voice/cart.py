"""The session cart: a list of line items held in conversation memory only, committed
with the conversation after a complete turn and gone with the session. The gateway writes the
confirmation sentence itself from the tool result, so the model cannot invent what was added.

The cart is not a reservation, but it never holds more of a variant than the catalog says is
available: add_to_cart counts what the cart already holds (`in_cart`), and the merge below
refuses anything past `available` as a second check."""

import re

# A request to change the cart, in the shopper's own words: add, the cart itself, buy, take, have,
# get, go with, hold. On a GPU smoke the model added a jacket unasked on "Actually, I want to stay
# under 100 dollars."; the gateway refuses add_to_cart on a turn whose words ask for no such change
# . Broad on purpose: a missed request only means the shopper asks again. "I want to" and
# "I'd like a" stay out, since they open most searches; "I want it" and "I'd like that one" count.
CART_REQUEST = re.compile(
    r"\b(add|adding|cart|basket|buy|purchase|order|grab|take|"
    r"get me|give me|ring (?:it|that|this|them) up|"
    r"(?:i(?:'|’)?ll|i(?:'|’)?d|i would|i will|we(?:'|’)?ll) (?:have|get|go with)|"
    r"(?:i(?:'|’)?d|i would) like (?:it|that|this|them|those|one|both|the)|"
    r"want (?:it|that|this|them|those|one|both|two|three|\d+)|"
    r"put (?:it|that|this|them|one) in|hold (?:it|that|this|one))\b",
    re.I,
)
# A plain yes counts only as the answer to the assistant's own cart question in the previous turn.
CONFIRMS = re.compile(r"^\W*(yes|yeah|yep|sure|ok|okay|please|do it|go ahead|absolutely)\b", re.I)
CART_OFFER = re.compile(r"\b(cart|basket)\b[^.?!]*\?", re.I)
NEGATED_REQUEST = re.compile(
    r"\b(?:don't|don’t|do not|never|stop|avoid|without)\b.{0,60}"
    r"\b(?:add|adding|buy|buying|order|ordering|take|taking|put|putting)\b|"
    r"\b(?:not now|not yet|later|tomorrow|no thanks|no thank you)\b",
    re.I,
)
READ_CART = re.compile(r"\b(?:show|what|view|list|remove|empty|clear)\b.*\b(?:cart|basket)\b", re.I)
QUANTITY_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
QUANTITY = re.compile(
    r"\b(?:add|buy|order|take|grab|want|give me|get me)\s+"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|both|all)\b",
    re.I,
)


def asks_to_change_cart(text, previous_reply=""):
    """Whether the shopper's message asks to add something to the cart, or says yes to the
    assistant's cart question that came right before it."""
    text = text or ""
    if NEGATED_REQUEST.search(text) or READ_CART.search(text):
        return False
    if CART_REQUEST.search(text):
        return True
    return bool(CONFIRMS.match(text) and CART_OFFER.search(previous_reply or ""))


def requested_quantity(text):
    """One unless the shopper explicitly asks for a supported count. Ambiguity fails closed."""
    matches = QUANTITY.findall(text)
    if not matches:
        return 1
    if len(matches) != 1:
        return None
    word = matches[0].lower()
    count = int(word) if word.isdigit() else QUANTITY_WORDS.get(word)
    return count if count is not None and 1 <= count <= 5 else None


LINE_KEYS = (
    "product_id",
    "product_name",
    "variant_id",
    "color",
    "size",
    "price_minor",
    "price_display",
    "quantity",
)


def merge_cart(cart, additions):
    """The cart after this turn's allowed add_to_cart results. Same variant twice adds up."""
    items = [dict(item) for item in cart]
    for output in additions:
        if not output.get("added"):
            continue
        line = next((item for item in items if item["variant_id"] == output["variant_id"]), None)
        held = line["quantity"] if line else 0
        if held + output["quantity"] > output.get("available", held + output["quantity"]):
            continue
        if line:
            line["quantity"] += output["quantity"]
        else:
            items.append({key: output[key] for key in LINE_KEYS})
    return items


def cart_total_minor(cart):
    return sum(item["price_minor"] * item["quantity"] for item in cart)


def price_display(minor):
    return f"${minor // 100:,}.{minor % 100:02d}"


def line_sentence(output):
    """One add_to_cart result in words: what was added, or why nothing was."""
    label = f"{output['product_name']} in {output['color']} {output['size']}"
    asked, available = output["quantity"], output["available"]
    held = output.get("in_cart", 0)
    verb = "is" if available == 1 else "are"
    if output["added"]:
        if asked == 1:
            return f"Added the {label}, {output['price_display']}, to your cart."
        return f"Added {asked} of the {label}, {output['price_display']} each, to your cart."
    if available == 0:
        return f"The {label} is out of stock at {output['store_name']}, so I did not add it."
    if held:
        sentence = f"You already have {held} in your cart and only {available} {verb} in stock"
        return sentence + ("." if held >= available else f", so I did not add {asked}.")
    return (
        f"Only {available} of the {label} {verb} in stock at {output['store_name']}, "
        f"so I did not add {asked}."
    )


def cart_answer(outputs, cart):
    """The spoken and displayed confirmation for this turn's add_to_cart results, in order,
    with the cart's count and total once something was added."""
    sentences = [line_sentence(output) for output in outputs]
    if any(output["added"] for output in outputs):
        count = sum(item["quantity"] for item in cart)
        items = "item" if count == 1 else "items"
        sentences.append(
            f"You have {count} {items} totaling {price_display(cart_total_minor(cart))}."
        )
    return " ".join(sentences)
