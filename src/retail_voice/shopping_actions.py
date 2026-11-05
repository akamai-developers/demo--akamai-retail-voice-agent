"""Resolved shopper actions. The model cannot substitute an item, a variant or an action."""

import re
import time
from dataclasses import replace

from .cart import cart_answer, merge_cart
from .speech_text import SIZE_NAMES
from .stock import APPAREL, listed, stock_answer, stock_clarification, variant_reply

YES = re.compile(
    r"^\W*(?:yes|yeah|yep|sure|okay|ok|please|go ahead|that works)(?:\s+please)?[.!\s]*$", re.I
)
NO = re.compile(r"\b(?:no|not|don't|don’t|do not|never|cancel|stop|later|instead)\b", re.I)


def continued_action(text, target, conversation):
    """Only a bounded answer to the immediately preceding action question can resume it."""
    pending = conversation.pending_action
    if not pending or NO.search(text) or pending.get("product_id") != target.product_id:
        return None, target
    if YES.fullmatch(text):
        proposal = pending.get("proposal")
        if proposal:
            target = replace(
                target,
                variant_id=proposal["variant_id"],
                colors=frozenset([proposal["color"]]),
                sizes=frozenset([proposal["size"]]),
            )
        return pending["kind"], target
    if variant_reply(text, target):
        return pending["kind"], target
    return None, target


async def perform_action(kind, target, quantity, *, catalog, conversation, tryon, call, trace):
    """Check the chosen variant before preview/cart. Alternatives never become selections."""
    tenant = conversation.tenant
    records: list = []
    pending = {"kind": kind, "product_id": target.product_id, "quantity": quantity}

    def result(answer, *, clarify=False, preview=None, proposal=None):
        action = dict(pending, proposal=proposal) if clarify else {}
        return dict(
            answer=answer,
            clarification=clarify,
            records=records,
            target=target,
            preview=preview,
            pending_action=action,
            clear_selection=not target.product_id,
        )

    if not target.product_id:
        return result(
            "Which item would you like to " + ("try on?" if kind == "preview" else "add?"),
            clarify=True,
        )
    card = catalog.card(tenant, target.product_id)
    if kind == "preview" and card["category"] not in APPAREL:
        return result("Try-on currently supports jackets, rain shells, fleeces and vests.")
    if kind == "cart" and target.store_id != conversation.store_id:
        return result(
            "Your cart uses this store's stock. Please choose an item at this store.", clarify=True
        )
    if quantity is None and kind == "cart":
        return result("How many should I add?", clarify=True)
    if not target.store_id or len(target.colors) > 1 or len(target.sizes) > 1:
        return result(stock_clarification(target, card), clarify=True)

    def check(variant):
        record = call(
            "check_availability",
            {
                "product_id": target.product_id,
                "variant_id": variant["variant_id"],
                "store_id": target.store_id,
            },
            "gateway-stock",
        )
        records.append(record)
        return record[2]

    def offer_alternative(opening):
        # Stay in the shopper's size. A checked alternative is proposed, never silently chosen.
        candidates = [
            v
            for v in card["variants"]
            if v["size"] in target.sizes and v["variant_id"] != target.variant_id
        ]
        available = [v for v in candidates if check(v)["in_stock"]]
        if len(available) == 1:
            v = available[0]
            label = f"{v['color']} {SIZE_NAMES.get(v['size'], v['size'])}"
            return result(
                opening + f" {label.capitalize()} is in stock. Would you like that instead?",
                clarify=True,
                proposal=v,
            )
        if available:
            return result(
                opening
                + " In your size, "
                + listed([v["color"] for v in available])
                + " are in stock. Which color would you like?",
                clarify=True,
            )
        return result(opening + " Would you like another size or item?", clarify=True)

    if not target.variant_id:
        if len(target.sizes) == 1 and not target.colors:
            size = next(iter(target.sizes))
            candidates = [v for v in card["variants"] if v["size"] == size]
            checked = [(v, check(v)) for v in candidates]
            available = [v for v, fact in checked if fact["in_stock"]]
            sold = [v["color"] for v, fact in checked if not fact["in_stock"]]
            size_name = SIZE_NAMES.get(size, size)
            if len(available) == 1:
                v = available[0]
                answer = f"In {size_name}, {v['color']} is in stock"
                answer += f" and {listed(sold)} is sold out." if sold else "."
                verb = "try" if kind == "preview" else "add"
                return result(
                    answer + f" Would you like to {verb} {v['color']}?", clarify=True, proposal=v
                )
            if available:
                return result(
                    f"In {size_name}, {listed([v['color'] for v in available])} are in stock. Which color would you like?",
                    clarify=True,
                )
        return result(stock_clarification(target, card), clarify=True)

    variant = next(v for v in card["variants"] if v["variant_id"] == target.variant_id)
    if kind == "cart":
        record = call(
            "add_to_cart",
            {
                "product_id": target.product_id,
                "variant_id": target.variant_id,
                "quantity": quantity,
            },
            "gateway-cart",
        )
        records.append(record)
        answer = cart_answer([record[2]], merge_cart(conversation.cart, [record[2]]))
        if record[2]["available"] == 0:
            return offer_alternative(answer)
        return result(answer)
    fact = check(variant)
    if not fact["in_stock"]:
        return offer_alternative(stock_answer(fact))
    if tryon is None or tryon.mode == "disabled" or tenant != tryon.tenant:
        return result("Try-on is not set up for this store yet.")

    cached = conversation.preview
    preview = (
        cached
        if cached.get("product_id") == target.product_id and cached.get("color") == variant["color"]
        else None
    )
    cache_hit = bool(preview)
    started = time.monotonic()
    trace.emit("preview.started", "running", preview_kind=tryon.mode, cache_hit=cache_hit)
    if not preview:
        preview = await tryon.create(tenant, target.product_id, variant["color"])
    trace.emit(
        "preview.completed",
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        preview_kind=tryon.mode,
        result_count=int(preview is not None),
        cache_hit=cache_hit,
    )
    if preview is None:
        return result("A preview is not available for that item and color yet.")
    label = "prepared preview" if preview["kind"] == "prepared" else "preview"
    name = (
        f"{card['name']} in {variant['color']} {SIZE_NAMES.get(variant['size'], variant['size'])}"
    )
    return result(
        f"The {name} is in stock. Here's your {label}, showing the look rather than the fit.",
        preview=preview,
    )
