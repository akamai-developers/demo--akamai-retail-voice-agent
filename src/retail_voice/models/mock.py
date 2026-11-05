"""Deterministic fixture backend. Every reply is labeled fixture output; audio is a test tone.

Like a real model, it sees only the messages and tool results it is given. It never reads
the catalog directly, so the tool boundary is exercised the same way in both modes.
"""

import asyncio
import io
import json
import math
import re
import struct
import wave
from typing import Any

from .base import Decision

KINDS = re.compile(
    r"\b(jackets?|shells?|rain|raincoats?|coats?|anoraks?|parkas?|fleeces?|hoodies?|vests?|"
    r"hats?|beanies?|caps?|gloves?|mittens?|mitts)\b"
)
BUDGET = re.compile(r"(?:under|below|less than|up to|max(?:imum)?)\s*\$?\s*(\d{1,5})")
# Read like stock.PRODUCT_ID: a whole dashed token, digits included ("jacket-a-123").
PRODUCT_ID = re.compile(r"\b(?:jacket-[ab](?:-[a-z0-9]+)*|[ab]-[a-z0-9]+(?:-[a-z0-9]+)+)\b")
COLORS = (
    "navy black gray grey charcoal red green olive blue yellow orange tan cream brown white".split()
)
SIZES = {"small": "S", "medium": "M", "large": "L", "extra large": "XL", "one size": "OS"}
AVAILABILITY = re.compile(r"\b(available|availability|in stock|stock|small|medium|large|size)\b")
COLOR_QUESTION = re.compile(r"\b(come in|color|colour|colors|colours)\b")
# Small talk the shopping prompt answers without tools: a bare greeting or thanks, the date or
# time, the weather.
GREETING = re.compile(
    r"\W*(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks|thank you)"
    r"(?: there| so much| very much)?\W*"
)
CLOCK = re.compile(
    r"\b(?:what(?:'s| is) (?:the )?(?:date|day|time)|what time|what day|today's date)\b"
)
WEATHER = re.compile(r"\b(?:weather|forecast|going to (?:rain|snow)|will it (?:rain|snow))\b")
# The cart: add the product under discussion, list the cart, or ask to pay.
# "Add five more.", "add 2 of those": a quantity of the settled variant (1 to 5, the tool's range).
ADD_MORE = re.compile(r"\badd (\d{1,2}|one|two|three|four|five) (?:more|of (?:them|those|these))\b")
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
CART = re.compile(
    r"\b(?:add (?:it|that|this|one|them|the [a-z ]{1,40}?) to (?:my |the )?cart|add to cart|"
    r"add (?:\d{1,2}|one|two|three|four|five) (?:more|of (?:them|those|these))|"
    r"put (?:it|that|this) in (?:my |the )?cart|i'?ll take (?:it|that|this|one)|"
    r"buy (?:it|that|this|one))\b"
)
CART_QUESTION = re.compile(
    r"\b(?:what(?:'s| is) in (?:my |the )?cart|show (?:me )?(?:my |the )?cart)\b"
)
CHECKOUT = re.compile(r"\b(?:check ?out|pay|purchase|order it|buy now)\b")
# A confirmation of the selected product: "yes, that's the one I'm looking for".
CONFIRM = re.compile(
    r"^\W*(?:yes|yeah|yep|ok|okay|sure|perfect|great|sounds good|that(?:'s| is) the one|"
    r"i'll take (?:it|that))\b"
)
# Words that make a message a product request rather than small talk. "Rain" alone is weather.
GARMENT = re.compile(
    r"\b(jackets?|shells?|raincoats?|coats?|anoraks?|parkas?|fleeces?|hoodies?|vests?|"
    r"hats?|beanies?|caps?|gloves?|mittens?|mitts|gear)\b"
)


def tone():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(1500 * math.sin(i * 2 * math.pi * 440 / 16000)))
                for i in range(4000)
            )
        )
    return buf.getvalue()


def recent_size(messages):
    """The size the shopper mentioned most recently in earlier turns."""
    start = max(i for i, m in enumerate(messages) if m["role"] == "user")
    for message in reversed(messages[:start]):
        if message["role"] == "user" and isinstance(message["content"], str):
            lowered = message["content"].lower()
            for word, code in SIZES.items():
                if word in lowered:
                    return code
    return None


def recent_color(messages):
    """The color the shopper mentioned most recently in earlier turns, as a model would recall."""
    start = max(i for i, m in enumerate(messages) if m["role"] == "user")
    for message in reversed(messages[:start]):
        if message["role"] == "user" and isinstance(message["content"], str):
            words = re.findall(r"[a-z]+", message["content"].lower())
            for word in reversed(words):
                if word in COLORS:
                    return "gray" if word == "grey" else word
    return None


def turn_state(messages):
    """The current user message and this turn's tool calls with their results."""
    start = max(i for i, m in enumerate(messages) if m["role"] == "user")
    calls, results = {}, []
    for message in messages[start + 1 :]:
        for call in message.get("tool_calls") or []:
            calls[call["id"]] = (
                call["function"]["name"],
                json.loads(call["function"]["arguments"]),
            )
        if message["role"] == "tool":
            name, args = calls[message["tool_call_id"]]
            results.append((name, args, json.loads(message["content"])))
    return messages[start], results


class MockBackend:
    mode = "mock"
    site_id = "local-mock"
    region = "local"
    profile_digest = "mock-fixture-v1"
    context_tokens = 16384

    async def ready(self):
        return True

    async def healthy(self):
        return await self.ready()

    def stage_metadata(self, stage):
        return {"model_id": "mock-" + stage.split(".")[0], "model_revision": "fixture-v1"}

    async def transcribe(self, audio):
        await asyncio.sleep(0)
        return "Fixture transcript: Does this jacket have a hood, and is navy medium in stock?"

    async def decide(self, messages, tools, tool_choice="auto"):
        await asyncio.sleep(0)
        names = {tool["function"]["name"] for tool in tools}
        if "lookup_inventory" in names:
            return self.legacy(messages)
        return self.shopping(messages, tool_choice)

    def legacy(self, messages):
        current, results = turn_state(messages)
        selected = json.loads(current["content"][0]["text"])
        sku = selected["selected_sku"]
        if not results:
            return Decision(
                tool="lookup_inventory",
                arguments={"sku": sku, "store_id": selected["selected_store"]},
                call_id="mock-call-1",
            )
        stock = results[-1][2]["navy_medium"]
        hood = "has" if sku == "jacket-a" else "does not have"
        return Decision(
            answer=f"Fixture answer: this jacket {hood} a hood. Navy medium stock: {stock}."
        )

    def call(self, results, name, arguments):
        return Decision(tool=name, arguments=arguments, call_id=f"mock-call-{len(results) + 1}")

    def shopping(self, messages, tool_choice):
        current, results = turn_state(messages)
        context = json.loads(current["content"])
        text = context["shopper_message"].lower()
        small = None if results else self.small_talk(messages, context, text)
        if small:
            return small
        if results and "error" in results[-1][2]:
            return Decision(answer="Fixture answer: I could not find that product in this store.")
        if tool_choice == "none":
            return self.summarize(results)
        # A product id typed or spoken by the shopper is looked up as given. For another
        # retailer's id this exercises the catalog denial, not a refusal by the model.
        requested = context.get("requested_product_ids") or []
        named = requested[0] if requested else None
        if named is None:
            match = PRODUCT_ID.search(text)
            named = match.group(0) if match else None
        if named:
            if not results:
                return self.call(results, "get_product_details", {"product_id": named})
            return self.summarize(results)
        pointed = context.get("referenced_product_id")
        earlier = context.get("selected_product_id")
        asks_product = AVAILABILITY.search(text) or COLOR_QUESTION.search(text)
        cart = self.cart_turn(results, context, text, messages)
        if cart:
            return cart
        if earlier and CONFIRM.search(text) and not KINDS.search(text) and not BUDGET.search(text):
            # "Yes, that's the one": no lookup, no product named, so the selection stays.
            return Decision(answer="Fixture answer: noted. Ask about its colors, sizes or stock.")
        if (
            pointed
            and not BUDGET.search(text)
            and not re.search(r"\b(show|find|need|want|looking|forget)\b", text)
        ):
            return self.product_question(results, context, text, pointed, messages)
        if "weekend" in text or not (KINDS.search(text) or BUDGET.search(text) or earlier):
            return Decision(
                clarification="Fixture question: what are you shopping for, "
                "a jacket, a fleece, a hat or gloves?"
            )
        if earlier and asks_product and not KINDS.search(text):
            return self.product_question(results, context, text, earlier, messages)
        if not results:
            return self.call(results, "search_products", self.search(text, context))
        found = next((r for n, _, r in results if n == "search_products"), None)
        sized = any(word in text for word in SIZES)
        if found and found["products"] and AVAILABILITY.search(text) and sized:
            # Search, then details, then stock, all in one turn.
            first = found["products"][0]["product_id"]
            return self.product_question(results, context, text, first, messages)
        return self.summarize(results)

    def cart_turn(self, results, context, text, messages):
        """Fixture handling of the cart rules: list it, decline checkout, or add the product
        under discussion in the color and size the shopper settled on."""
        if CART_QUESTION.search(text) and not CART.search(text):
            items = context.get("cart") or []
            if not items:
                return Decision(answer="Fixture answer: your cart is empty.")
            listed = "; ".join(
                f"{i['product_name']} in {i['color']} {i['size']} x{i['quantity']}" for i in items
            )
            return Decision(answer=f"Fixture answer: your cart has {listed}.")
        if CHECKOUT.search(text) and not CART.search(text):
            return Decision(
                answer="Fixture answer: there is no checkout here. A store associate completes "
                "the purchase from your cart."
            )
        if not CART.search(text):
            return None
        if results and results[-1][0] == "add_to_cart":
            return self.summarize(results)
        focus = context.get("product_in_focus")
        if not focus:
            return Decision(clarification="Fixture question: which product should I add?")
        color = next((c for c in COLORS if re.search(rf"\b{c}\b", text)), None)
        color = "gray" if color == "grey" else color
        size = next((code for word, code in SIZES.items() if word in text), None)
        settled = focus.get("selected_variant_id")
        more = ADD_MORE.search(text)
        quantity = {}
        if more:
            asked = NUMBER_WORDS.get(more.group(1)) or int(more.group(1))
            quantity = {"quantity": min(max(asked, 1), 5)}
        if settled and not color and not size:
            variant = settled
        else:
            color = color or recent_color(messages)
            size = size or recent_size(messages)
            pick = next(
                (
                    v
                    for v in focus["variants"]
                    if (color is None or v["color"] == color)
                    and (size is None or v["size"] == size)
                ),
                None,
            )
            if pick is None:
                return Decision(
                    clarification="Fixture question: which color and size should I add?"
                )
            variant = pick["variant_id"]
        return self.call(
            results,
            "add_to_cart",
            {"product_id": focus["product_id"], "variant_id": variant} | quantity,
        )

    def small_talk(self, messages, context, text):
        """Fixture replies for the prompt's small-talk rules. No tool, no product named. A
        message that asks for a product ("a jacket for rainy weather") is shopping instead."""
        if GARMENT.search(text) or BUDGET.search(text) or PRODUCT_ID.search(text):
            return None
        if GREETING.fullmatch(text):
            store = re.search(r"The shopper is at ([^.]+)\.", messages[0]["content"])
            where = f" to {store.group(1)}" if store else ""
            return Decision(
                answer=f"Fixture answer: hello, welcome{where}. Tell me what you are shopping for."
            )
        if CLOCK.search(text):
            return Decision(
                answer=f"Fixture answer: it is {context['local_time']} at the store "
                f"({context['timezone']})."
            )
        if WEATHER.search(text):
            return Decision(
                answer="Fixture answer: I cannot check the weather here. "
                "I can show you rain jackets or warm layers."
            )
        return None

    def search(self, text, context):
        prefs = context.get("known_preferences") or {}
        budget = BUDGET.search(text)
        query = BUDGET.sub(" ", text)
        query = re.sub(r"\b(actually|dollars?|bucks|i need|i want|show me|please)\b", " ", query)
        query = " ".join(query.split())[:200]
        if not KINDS.search(query) and prefs.get("last_query"):
            query = prefs["last_query"]
        arguments: dict[str, Any] = {"query": query}
        if budget:
            arguments["max_price_minor"] = int(budget.group(1)) * 100
        elif re.search(r"\b(forget|ignore|drop) the budget\b|\bany price\b", text):
            pass
        elif "max_price_minor" in prefs:
            arguments["max_price_minor"] = prefs["max_price_minor"]
        return arguments

    def product_question(self, results, context, text, target, messages):
        details = next((r for n, _, r in results if n == "get_product_details"), None)
        if details is None:
            return self.call(results, "get_product_details", {"product_id": target})
        product = details["product"]
        colors = list(dict.fromkeys(v["color"] for v in product["variants"]))
        asked_color = next((c for c in COLORS if re.search(rf"\b{c}\b", text)), None)
        asked_color = "gray" if asked_color == "grey" else asked_color
        asked_size = next((code for word, code in SIZES.items() if word in text), None)
        if asked_size is None:
            name = product["name"]
            if asked_color in colors:
                return Decision(answer=f"Fixture answer: yes, the {name} comes in {asked_color}.")
            listed = ", ".join(colors)
            return Decision(answer=f"Fixture answer: the {name} comes in {listed}.")
        stock = next((r for n, _, r in results if n == "check_availability"), None)
        if stock is not None:
            where = stock["store_name"]
            count = stock["quantity"]
            label = f"{stock['color']} {stock['size']}"
            if count:
                return Decision(
                    answer=f"Fixture answer: {product['name']} in {label}: {count} in stock at {where}."
                )
            return Decision(
                answer=f"Fixture answer: {product['name']} in {label} is out of stock at {where}."
            )
        wanted = (
            asked_color
            or recent_color(messages)
            or (context.get("known_preferences") or {}).get("color")
        )
        sized = [v for v in product["variants"] if v["size"] == asked_size]
        if not sized:
            return Decision(
                answer=f"Fixture answer: the {product['name']} does not come in size {asked_size}."
            )
        variant = next((v for v in sized if v["color"] == wanted), sized[0])
        arguments = {"product_id": product["product_id"], "variant_id": variant["variant_id"]}
        return self.call(results, "check_availability", arguments)

    def summarize(self, results):
        if not results:
            return Decision(answer="Fixture answer: I could not check the catalog for that.")
        name, _, output = results[-1]
        if name == "get_product_details":
            product = output["product"]
            colors = ", ".join(dict.fromkeys(v["color"] for v in product["variants"]))
            return Decision(
                answer=f"Fixture answer: the {product['name']} costs {product['price_display']} "
                f"and comes in {colors}."
            )
        if name == "check_availability":
            return Decision(
                answer=f"Fixture answer: {output['product_name']} in {output['color']} "
                f"{output['size']}: {output['quantity']} in stock at {output['store_name']}."
            )
        if name == "add_to_cart":
            # The gateway writes the real confirmation from the tool result.
            return Decision(answer="Fixture answer: added it to your cart.")
        if not output["products"]:
            return Decision(
                answer="Fixture answer: I found no matches. Try a higher budget or another style."
            )
        first = output["products"][0]
        count = output["match_count"]
        if count == 1:
            return Decision(
                answer=f"Fixture answer: I found 1 option: the {first['name']} at "
                f"{first['price_display']}."
            )
        # Several matches: name none, so the fixture selects nothing (the selection logic selects the one shown
        # product a reply is about).
        return Decision(
            answer=f"Fixture answer: I found {count} options from {first['price_display']}."
        )

    async def synthesize(self, text):
        await asyncio.sleep(0)
        return tone()

    async def close(self):
        pass
