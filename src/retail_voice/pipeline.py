"""One orchestrator for both turn types.

`run_turn` is the legacy diagnostic turn (fixed SKU, store and photo) behind `/turns` and
`/fixture-turn`. `run_message` is a shopping conversation turn. Both share the permit wait,
the Pipecat stages, the deadline, cancellation and the single terminal event.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .cart import (
    CART_REQUEST,
    NEGATED_REQUEST,
    asks_to_change_cart,
    cart_answer,
    merge_cart,
    requested_quantity,
)
from .catalog import execute_tool, explicit_search_budget, result_count, search_arguments
from .catalog_repository import CATEGORY_WORDS
from .conversation import (
    REPLY_TOKENS,
    TOOL_RESERVE_TOKENS,
    TOOL_SCHEMA_TOKENS,
    Reference,
    estimate_tokens,
)
from .errors import ContextLimit, Denied, ToolArgumentError, ToolBudgetExhausted
from .events import Trace
from .models.base import Backend
from .models.nvidia import ModelRejected
from .models.tools import LEGACY_INVENTORY_TOOL, SHOPPING_TOOL_NAMES, shopping_tools
from .orchestration import run_stages
from .prompts import legacy_messages, shopping_message, shopping_system, tool_exchange
from .shopping_actions import continued_action, perform_action
from .speech_text import spoken
from .stock import (
    STOCK_LANGUAGE,
    color_question,
    named_product_ids,
    stock_answer,
    stock_target,
    validate_stock_answer,
    variant_reply,
)
from .stock import (
    stock_clarification as clarification_sentence,
)
from .tryon import requested as tryon_requested

log = logging.getLogger(__name__)
MAX_DECISIONS = 5
MAX_TOOL_CALLS = 4
# Sent once when a reply carried tool markup or reasoning tags, keyed by the tool choice that
# the retry runs with. After four tool calls the model has to answer in words.
MALFORMED_HINT = {
    "auto": (
        "Your last reply was not shown: it contained tool markup or reasoning tags. Reply "
        "again in plain English sentences with no tags, or call a tool through the tools "
        "interface only."
    ),
    "none": (
        "Your last reply was not shown: it contained tool markup or reasoning tags. Reply "
        "again in plain English sentences with no tags. The tools are not available for this "
        "reply, so say what you could and could not confirm."
    ),
}
LEGACY_DECISIONS = 3
CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
TOOL_LABELS = {"lookup_inventory", *SHOPPING_TOOL_NAMES}
REASONS = (
    (ContextLimit, "context_limit"),
    (ToolBudgetExhausted, "tool_budget_exhausted"),
    (ModelRejected, "model_rejected_request"),
)


@dataclass
class Message:
    """One shopper message for the conversation route: audio or text, never both."""

    audio: bytes | None = None
    text: str | None = None
    selected_product_id: str | None = None
    display_revision: int | None = None

    @property
    def input_source(self):
        return "audio" if self.audio is not None else "text"


@dataclass
class Outcome:
    answer: str
    clarification: bool = False
    records: list = field(default_factory=list)  # (tool name, arguments, output, allowed)
    reference: Any = None
    speech: str | None = None
    preview: dict | None = None
    target: Any = None
    pending_action: dict = field(default_factory=dict)
    clear_selection: bool = False


def elapsed_ms(start):
    """Milliseconds on the gateway's monotonic clock since `start`."""
    return round((time.monotonic() - start) * 1000, 1)


async def _run(backend, session, *, deadline, on_event, permit, audio, text, source, agent, finish):
    trace = Trace(session, backend, on_event)
    opened = time.monotonic()
    result: dict[str, Any] = {
        "turn_id": trace.turn_id,
        "trace_id": trace.id,
        "mode": backend.mode,
        "events": trace.events,
        "status": "failed",
        "answer": "",
        "transcript": "",
        "audio": b"",
    }
    labels = {"input_source": source} if source else {}
    trace.emit("turn.started", "running", **labels)

    async def stage(name, fn, describe=None):
        start = time.monotonic()
        value = await fn()
        attrs = backend.stage_metadata(name) | (describe(value) if describe else {})
        trace.emit(name, duration_ms=(time.monotonic() - start) * 1000, **attrs)
        return value

    heard = {"text": text}
    outcome: dict[str, Outcome] = {}

    async def asr():
        if audio is not None:
            heard["text"] = await stage("asr.completed", lambda: backend.transcribe(audio))

    async def think():
        outcome["value"] = await agent(trace, stage, heard["text"])

    async def tts():
        answer = outcome["value"].answer
        speech = outcome["value"].speech or answer
        voice = await stage("tts.completed", lambda: backend.synthesize(spoken(speech)))
        result.update(status="complete", answer=answer, audio=voice, spoken_answer=speech)

    async def wait_for_permit():
        # Queue time counts toward the deadline. A waiting turn reaches no model, so a
        # cancel or timeout here never touches the running turn or its quarantine marker.
        if permit is None or not permit.waiting:
            return
        start = time.monotonic()
        trace.emit("turn.waiting", "waiting")
        try:
            await permit.granted.wait()
        finally:
            result["queued_ms"] = round((time.monotonic() - start) * 1000, 1)
        trace.emit("turn.waiting", queued_ms=result["queued_ms"])

    async def execute():
        await wait_for_permit()
        await run_stages((asr, think, tts))

    try:
        await asyncio.wait_for(execute(), deadline)
    except asyncio.CancelledError:
        result["reason"] = "cancelled"
    except TimeoutError:
        result["reason"] = "deadline"
    except Denied as exc:
        result["reason"] = str(exc)
    except Exception as exc:
        result["reason"] = next((code for kind, code in REASONS if isinstance(exc, kind)), None)
        result["reason"] = result["reason"] or "backend_failed"
        # Operators need more than "backend_failed". Only a fixed code is logged, never text.
        code = str(exc) if re.fullmatch(r"[a-z_]{1,64}", str(exc)) else "(detail withheld)"
        log.warning("turn %s failed: %s %s", trace.turn_id, type(exc).__name__, code)
    finish(result, heard["text"], outcome.get("value"))
    trace.emit(
        "turn.completed" if result["status"] == "complete" else "turn.failed",
        result["status"],
        # From turn.started to this event on the gateway's clock: queue wait, every stage and
        # validation. The upload happened before the turn started and is not included.
        duration_ms=elapsed_ms(opened),
        result_path=f"/v1/sessions/{session.id}/turns/{trace.turn_id}/result",
        # The reply audio's route, so a browser can fetch it alongside the result instead of
        # after it. The result stays the source of truth for the text and the cards.
        **(
            {
                "audio_path": f"/v1/sessions/{session.id}/turns/{trace.turn_id}/audio",
                "audio_kind": "test_tone" if backend.mode == "mock" else "synthesized_speech",
            }
            if result["status"] == "complete" and result.get("audio")
            else {}
        ),
        **({"reason": result["reason"]} if "reason" in result else {}),
        **({"queued_ms": result["queued_ms"]} if "queued_ms" in result else {}),
        **labels,
    )
    return result


async def run_turn(
    backend: Backend, session, audio, image, sku, store, deadline=30, on_event=None, permit=None
):
    """Legacy diagnostic turn: one inventory lookup for a fixed SKU and store, with a photo."""

    async def agent(trace, stage, transcript):
        messages = legacy_messages(transcript, image, sku, store)
        records: list = []
        for _ in range(LEGACY_DECISIONS):
            choice = "none" if records else "auto"
            decision = await stage(
                "model.completed",
                lambda: backend.decide(messages, [LEGACY_INVENTORY_TOOL], choice),
                lambda d: d.usage or {},
            )
            if decision.tool:
                try:
                    output = execute_tool(session.tenant, decision.tool, decision.arguments or {})
                except Denied as exc:
                    trace.emit("tool.denied", "denied", reason=str(exc))
                    raise
                call_id = decision.call_id or "catalog-call"
                records.append((decision.tool, decision.arguments, output, True))
                messages = messages + tool_exchange(
                    call_id, decision.tool, decision.arguments or {}, output
                )
                trace.emit("tool.allowed")
            elif decision.text and records:
                return Outcome(answer=decision.text, records=records)
            else:
                raise ValueError("missing_tool_or_answer")
        raise ValueError("tool_round_limit")

    def finish(result, transcript, outcome):
        if result["status"] == "complete":
            result.update(transcript=transcript, tool_result=outcome.records[-1][2])

    return await _run(
        backend,
        session,
        deadline=deadline,
        on_event=on_event,
        permit=permit,
        audio=audio,
        text=None,
        source=None,
        agent=agent,
        finish=finish,
    )


def decision_attrs(decision):
    """What a model.completed event may say about a decision: its kind and token counts."""
    kind = "malformed" if decision.malformed else "tool_call" if decision.tool else "answer"
    return {"decision": kind} | (decision.usage or {})


def message_tokens(message):
    return estimate_tokens(message.get("content") or message.get("tool_calls") or "")


HINTS = {
    "search_products": "Pass query, and optionally category, color, size, max_price_minor "
    "(integer cents; 0 or absent means no budget) and limit 1 to 3.",
    "get_product_details": "Pass product_id exactly as a tool returned it.",
    "check_availability": "Pass product_id and an exact variant_id from a tool result, for "
    "example jacket-a-navy-m. Optional store_id is one of: {stores}.",
    "add_to_cart": "Pass product_id and an exact variant_id from product_in_focus or a tool "
    "result, and optionally quantity 1 to 5.",
}


def variant_hint(catalog, tenant, arguments):
    """Valid variant ids when the tenant owns the product. Unknown or foreign products get
    nothing, so the reply for them stays identical."""
    product_id = arguments.get("product_id") if isinstance(arguments, dict) else None
    if not isinstance(product_id, str) or not catalog.owns_product(tenant, product_id):
        return None
    variants = catalog.card(tenant, product_id)["variants"]
    return "Valid variant_id values for this product: " + ", ".join(
        f"{v['variant_id']} ({v['color']} {v['size']})" for v in variants
    )


def clarification_for(target, catalog, tenant):
    """The gateway's stock clarification for this turn, built from the shopper's resolved
    product when there is one (see stock_clarification)."""
    if (
        target is None
        or not target.product_id
        or not catalog.owns_product(tenant, target.product_id)
    ):
        return None
    return clarification_sentence(target, catalog.card(tenant, target.product_id))


def tool_error(exc, name=None, stores=(), variants=None):
    if str(exc) in {"cart_target_unresolved", "cart_target_mismatch"}:
        return str(exc), {
            "error": str(exc),
            "hint": "Do not guess the item, variant or quantity. Ask the shopper to clarify.",
        }
    if str(exc) == "variant_not_selected":
        hint = (
            "No exact variant was chosen by the shopper. Do not guess a color or size. "
            "For a color or product question, use get_product_details and answer that "
            "question. For stock, say what this product comes in and ask only for the "
            "missing color or size."
        )
        if variants:
            hint += " " + variants
        return "variant_not_selected", {"error": "variant_not_selected", "hint": hint}
    if isinstance(exc, ToolArgumentError):
        # A short fix-it hint lets the model retry within its budget. It names only this
        # tenant's own stores, which the tool schema already lists.
        hint = HINTS.get(name, "Use one of the listed tools.").format(stores=", ".join(stores))
        if variants:
            hint += " " + variants
        return "tool_arguments_denied", {"error": "invalid_arguments", "hint": hint}
    if variants and name == "check_availability":
        # The product is the tenant's own; only the variant or store was wrong.
        return str(exc), {"error": "not_found", "hint": variants}
    if str(exc) == "tool_scope_denied":
        return "tool_scope_denied", {"error": "unknown_tool"}
    if str(exc) == "cart_not_requested":
        return "cart_not_requested", {
            "error": "not_requested",
            "hint": "The shopper did not ask to add anything. Do not change the cart; answer "
            "the shopper's message.",
        }
    # Unknown and foreign product, variant and store ids get the same answer.
    return str(exc), {"error": "not_found"}


def owns_id(catalog, tenant, token):
    """A product id or variant id that belongs to this tenant's catalog."""
    if catalog.owns_product(tenant, token):
        return True
    pair = catalog.variants.get(token)
    return pair is not None and pair[0].tenant_id == tenant


def display_after(conversation, catalog, outcome):
    """Card order, selection and preferences after a successful turn, from allowed tools only."""
    displayed = conversation.displayed
    selected, variant = conversation.selected_product_id, conversation.selected_variant_id
    preferences: dict[str, Any] | None = None  # replaced by the latest search, else unchanged
    searched, touched, variant_now, variant_product = None, [], None, None
    for name, arguments, output, allowed in outcome.records:
        if not allowed:
            continue
        if name == "search_products":
            searched = tuple(p["product_id"] for p in output["products"])
            query, filters, _ = search_arguments(
                {k: v for k, v in arguments.items() if v is not None}
            )
            # The latest search defines the remembered filters, so a dropped budget is forgotten.
            preferences = {**filters, "last_query": query}
        elif name == "get_product_details":
            touched.append(output["product"]["product_id"])
        elif name == "check_availability":
            if outcome.target is not None and not outcome.target.matches(output):
                continue
            touched.append(output["product_id"])
            variant_now = output["variant_id"]
            variant_product = output["product_id"]
        elif name == "add_to_cart":
            # The product just added is the one under discussion; its variant is settled.
            touched.append(output["product_id"])
            if output["added"]:
                variant_now = output["variant_id"]
                variant_product = output["product_id"]
    pointed = outcome.reference.product_id if outcome.reference else None
    positional = pointed is not None and bool(getattr(outcome.reference, "positional", False))
    if searched is not None:
        displayed = searched
        if not touched:
            selected, variant = None, None
    if positional and touched and pointed not in touched:
        # "The second one" was resolved by the store. A lookup of some other product in the
        # same turn does not move the selection, so the next stock question stays on it.
        focus = pointed
    elif touched:
        focus = touched[-1]
    else:
        # No lookup. A spoken position stands as said. Otherwise the selection follows the one
        # shown product the reply talks about, so "it" means the product just discussed rather
        # than a card clicked earlier, and a descriptive reference ("the green one at $69")
        # that the model understood selects that card, with or without a prior selection and
        # even when this turn searched. A reply naming several products, or none, leaves
        # the selection alone.
        focus = pointed
        if not positional:
            named = named_products(outcome.answer, catalog, conversation.tenant, displayed)
            if len(named) == 1:
                focus = named[0]
    if focus is not None:
        if focus != selected:
            variant = None
        selected = focus
        if searched is None and focus not in displayed:
            displayed = (focus,)
    if variant_now is not None and variant_product == selected:
        variant = variant_now
    if outcome.target and outcome.target.product_id and searched is None:
        target = outcome.target
        if selected != target.product_id:
            variant = None
        selected = target.product_id
        if selected not in displayed:
            displayed = (selected,)
        if target.variant_id and not outcome.clarification:
            variant = target.variant_id
        elif target.variant_id != variant:
            variant = None
    if outcome.clear_selection:
        selected, variant = None, None
    revision = conversation.display_revision + (displayed != conversation.displayed)
    return displayed, revision, selected, variant, preferences


def named_products(answer, catalog, tenant, product_ids):
    """Products from `product_ids` (server-known, tenant-owned) named in the answer, in order
    of first mention."""
    found: list[tuple[int, str]] = []
    lowered = answer.lower()
    for product_id in product_ids:
        if product_id in (f[1] for f in found):
            continue
        card = catalog.card(tenant, product_id)
        spots = [lowered.find(card["name"].lower()), lowered.find(product_id)]
        spots = [s for s in spots if s >= 0]
        if spots:
            found.append((min(spots), product_id))
    return [product_id for _, product_id in sorted(found)]


def mentioned(answer, catalog, tenant, outcome):
    """Products named in the answer that a tool returned in this turn. Others are dropped."""
    ids: list[str] = []
    for name, _, output, allowed in outcome.records:
        if not allowed:
            continue
        if name == "search_products":
            ids.extend(p["product_id"] for p in output["products"])
        else:
            ids.append(
                output["product"]["product_id"] if "product" in output else output["product_id"]
            )
    return named_products(answer, catalog, tenant, ids)


async def run_message(
    backend: Backend,
    session,
    message: Message,
    catalog,
    deadline=30,
    on_event=None,
    permit=None,
    tryon=None,
):
    """One shopping turn: optional ASR, a bounded tool loop over scoped tools, then TTS."""
    conversation = session.conversation
    tenant = session.tenant
    stores = [s.store_id for s in catalog.tenant_stores(tenant)]
    tools = shopping_tools(stores)

    # A selected product must belong to this tenant. The API rejects others; this is a backstop.
    selected = message.selected_product_id
    if selected is not None and not catalog.owns_product(tenant, selected):
        selected = None

    resolved = {"target": None}

    async def respond(trace, stage, transcript):
        reference = conversation.resolve_reference(transcript, message.display_revision, selected)
        if reference.clarify:
            # A stale or ambiguous pointer gets a fixed question. No model or tool runs.
            return Outcome(answer=reference.clarify, clarification=True, reference=reference)
        target = stock_target(transcript, catalog, conversation, reference)
        resolved["target"] = target
        if target.product_id and not reference.positional:
            reference = Reference(product_id=target.product_id)

        def catalog_call(name, arguments, call_id):
            started = time.monotonic()
            output = execute_tool(
                tenant,
                name,
                arguments,
                repository=catalog,
                stores=stores,
                default_store=conversation.store_id,
                cart=conversation.cart,
            )
            trace.emit(
                "tool.completed",
                tool_name=name,
                tool_call_id=call_id,
                result_count=result_count(name, output),
                catalog_version=catalog.catalog_version,
                duration_ms=elapsed_ms(started),
            )
            return (name, arguments, output, True)

        def exact_stock():
            return catalog_call(
                "check_availability",
                {
                    "product_id": target.product_id,
                    "variant_id": target.variant_id,
                    "store_id": target.store_id,
                },
                "gateway-stock",
            )

        prefix = "Fixture answer: " if backend.mode == "mock" else ""
        if NEGATED_REQUEST.search(transcript) and CART_REQUEST.search(transcript):
            return Outcome(prefix + "Okay, I have not changed your cart.", reference=reference)
        cart_requested = asks_to_change_cart(
            transcript, conversation.exchanges[-1].assistant if conversation.exchanges else ""
        )
        cart_quantity = requested_quantity(transcript)
        action = "preview" if tryon_requested(transcript) else "cart" if cart_requested else None
        if action is None:
            action, target = continued_action(transcript, target, conversation)
            if action == "cart":
                cart_quantity = conversation.pending_action.get("quantity", 1)
        if action is not None:
            resolved["target"] = target
            values = await perform_action(
                action,
                target,
                cart_quantity,
                catalog=catalog,
                conversation=conversation,
                tryon=tryon,
                call=catalog_call,
                trace=trace,
            )
            values["answer"] = prefix + values["answer"]
            return Outcome(reference=reference, **values)
        if (
            STOCK_LANGUAGE.search(transcript)
            and re.search(r"\b(?:thought|you said|she said|told me)\b", transcript, re.I)
            and target.variant_id
            and target.store_id
        ):
            record = exact_stock()
            return Outcome(
                prefix + "Sorry for the confusion. " + stock_answer(record[2]),
                records=[record],
                reference=reference,
            )
        if color_question(transcript, target):
            record = catalog_call(
                "get_product_details", {"product_id": target.product_id}, "gateway-details"
            )
            card = catalog.card(tenant, target.product_id)
            if target.colors <= set(card["colors"]) and len(target.colors) == 1:
                answer = f"Yes, the {card['name']} comes in {next(iter(target.colors))}."
                return Outcome(prefix + answer, records=[record], reference=reference)
            return Outcome(
                prefix + clarification_for(target, catalog, tenant), True, [record], reference
            )
        if variant_reply(transcript, target):
            if target.variant_id and target.store_id:
                record = exact_stock()
                return Outcome(
                    prefix + stock_answer(record[2]), records=[record], reference=reference
                )
            return Outcome(
                prefix + clarification_for(target, catalog, tenant), True, reference=reference
            )
        search_budget = None if cart_requested else explicit_search_budget(transcript)
        # Ids the shopper typed or spoke, as given. One the store does not own gets a fixed
        # reply whatever the model does with it.
        named_ids = named_product_ids(transcript)
        foreign = [i for i in named_ids if not owns_id(catalog, tenant, i)]
        # The details tool takes product ids, so a variant id is shown to the model as its
        # product; the stock target keeps the exact variant.
        requested = list(
            dict.fromkeys(
                catalog.variants[i][0].product_id if i in catalog.variants else i for i in named_ids
            )
        )
        system = shopping_system(catalog, conversation)
        # The model sees the resolved target only for a stock question. On a budget change it
        # would otherwise check that variant instead of searching again.
        current = shopping_message(
            transcript,
            message.input_source,
            catalog,
            conversation,
            reference,
            target if STOCK_LANGUAGE.search(transcript) else None,
            requested,
        )
        fixed = (
            estimate_tokens(system["content"])
            + max(TOOL_SCHEMA_TOKENS, 2 * estimate_tokens(tools))
            + estimate_tokens(current["content"])
            + REPLY_TOKENS
        )
        if fixed > backend.context_tokens:
            raise ContextLimit("context_limit")
        budget = max(0, backend.context_tokens - fixed - TOOL_RESERVE_TOKENS)
        history, dropped = conversation.history_for_model(budget)
        if dropped:
            trace.emit(
                "context.trimmed", dropped_exchanges=dropped, kept_exchanges=len(history) // 2
            )
        records: list = []
        exchange: list = []
        decisions = calls = made = 0
        # The tools this turn still offers. A refused cart change takes add_to_cart away and
        # makes the rest of the turn words only.
        offered = tools
        refused_cart = False
        cart_refusal = None
        retried = False
        while True:
            # Re-check the whole prompt before every decision; tool output can push it over.
            trimmed = False
            while (
                fixed + sum(message_tokens(m) for m in history + exchange) > backend.context_tokens
            ):
                if not history:
                    raise ContextLimit("context_limit")
                history, dropped, trimmed = history[2:], dropped + 1, True
            if trimmed:
                trace.emit(
                    "context.trimmed", dropped_exchanges=dropped, kept_exchanges=len(history) // 2
                )
            if decisions >= MAX_DECISIONS:
                raise ToolBudgetExhausted("tool_budget_exhausted")
            choice = "auto" if calls < MAX_TOOL_CALLS and not refused_cart else "none"
            prompt = [system, *history, current, *exchange]
            decision = await stage(
                "model.completed",
                lambda: backend.decide(prompt, offered, choice),
                decision_attrs,
            )
            decisions += 1
            if decision.malformed:
                # The text carried tool markup or reasoning tags. One plain-words retry within
                # the same budget; a second one fails the turn. The withheld text is not sent
                # back, so nothing in it is parsed or echoed.
                if retried:
                    raise ValueError(decision.malformed)
                retried = True
                exchange = exchange + [{"role": "user", "content": MALFORMED_HINT[choice]}]
                continue
            if decision.tool:
                if calls >= MAX_TOOL_CALLS:
                    raise ToolBudgetExhausted("tool_budget_exhausted")
                calls += 1
                made += 1
                call_id = decision.call_id
                if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
                    call_id = f"call-{made}"
                label = decision.tool if decision.tool in TOOL_LABELS else "unknown"
                arguments = decision.arguments if isinstance(decision.arguments, dict) else None
                called = time.monotonic()
                try:
                    if decision.tool not in SHOPPING_TOOL_NAMES:
                        raise Denied("tool_scope_denied")
                    if arguments is None:
                        # The argument string was not a JSON object. The hint lets the model
                        # retry inside its budget instead of failing a repeatable greedy turn.
                        raise ToolArgumentError("invalid_arguments")
                    if decision.tool == "add_to_cart" and not cart_requested:
                        # The shopper's words asked for no cart change: the model may not
                        # write the cart on a budget change or a stock question.
                        raise Denied("cart_not_requested")
                    if decision.tool == "add_to_cart":
                        if not target.product_id or not target.variant_id or cart_quantity is None:
                            cart_refusal = (
                                "How many should I add?"
                                if cart_quantity is None
                                else clarification_for(target, catalog, tenant).replace(
                                    "check", "add"
                                )
                                if target.product_id
                                else "Which item would you like me to add?"
                            )
                            raise Denied("cart_target_unresolved")
                        already_added = sum(
                            r[2]["quantity"]
                            for r in records
                            if r[0] == "add_to_cart" and r[3] and r[2].get("added")
                        )
                        if (
                            arguments.get("product_id") != target.product_id
                            or arguments.get("variant_id") != target.variant_id
                            or type(arguments.get("quantity", 1)) is not int
                            or arguments.get("quantity", 1) != cart_quantity - already_added
                        ):
                            cart_refusal = (
                                "I did not add that item. Please confirm the item and quantity."
                            )
                            raise Denied("cart_target_mismatch")
                    if decision.tool == "search_products" and search_budget is not None:
                        # The shopper's stated ceiling wins over a stale model argument.
                        arguments = dict(arguments, max_price_minor=search_budget)
                    output = execute_tool(
                        tenant,
                        decision.tool,
                        arguments,
                        stores=stores,
                        default_store=conversation.store_id,
                        repository=catalog,
                        # The committed cart plus this turn's earlier adds, so repeated adds
                        # never pass what the catalog says is available (N1).
                        cart=merge_cart(
                            conversation.cart,
                            [r[2] for r in records if r[0] == "add_to_cart" and r[3]],
                        ),
                    )
                    if (
                        decision.tool == "check_availability"
                        and not target.variant_id
                        and catalog.owns_product(tenant, arguments.get("product_id"))
                    ):
                        raise Denied("variant_not_selected")
                except Denied as exc:
                    hint = (
                        variant_hint(catalog, tenant, arguments)
                        if decision.tool == "check_availability"
                        else None
                    )
                    reason, output = tool_error(exc, decision.tool, stores, hint)
                    trace.emit(
                        "tool.denied",
                        "denied",
                        reason=reason,
                        tool_name=label,
                        tool_call_id=call_id,
                        duration_ms=elapsed_ms(called),
                    )
                    records.append((decision.tool, arguments, output, False))
                    if reason in {
                        "cart_not_requested",
                        "cart_target_unresolved",
                        "cart_target_mismatch",
                    }:
                        # Refused cart changes consume no tool budget. Require a text reply
                        # next so repeated add attempts cannot exhaust the turn.
                        calls -= 1
                        refused_cart = True
                        offered = [t for t in offered if t["function"]["name"] != "add_to_cart"]
                else:
                    trace.emit(
                        "tool.completed",
                        tool_name=label,
                        tool_call_id=call_id,
                        result_count=result_count(decision.tool, output),
                        catalog_version=catalog.catalog_version,
                        duration_ms=elapsed_ms(called),
                    )
                    records.append((decision.tool, arguments, output, True))
                exchange = exchange + tool_exchange(call_id, decision.tool, arguments, output)
                continue
            text = (decision.text or "").strip()
            if not text:
                raise ValueError("missing_tool_or_answer")
            clarification = decision.clarification is not None or (
                calls == 0 and text.endswith("?")
            )
            if (
                STOCK_LANGUAGE.search(transcript)
                and target.product_id
                and target.variant_id
                and target.store_id
                and not any(
                    name == "check_availability" and allowed and target.matches(output)
                    for name, _, output, allowed in records
                )
            ):
                # The gateway resolved the exact product, variant and store, and the model
                # answered without checking it or checked something else (greedy decoding did
                # this on one smoke step in two of four runs). A fresh catalog check answers
                # the stock question; the trace records it as the gateway's own call.
                arguments = {
                    "product_id": target.product_id,
                    "variant_id": target.variant_id,
                    "store_id": target.store_id,
                }
                called = time.monotonic()
                try:
                    output = execute_tool(
                        tenant,
                        "check_availability",
                        arguments,
                        stores=stores,
                        default_store=conversation.store_id,
                        repository=catalog,
                    )
                except (Denied, ToolArgumentError):
                    output = None
                if output is not None:
                    trace.emit(
                        "tool.completed",
                        tool_name="check_availability",
                        tool_call_id="gateway-stock",
                        result_count=1,
                        catalog_version=catalog.catalog_version,
                        duration_ms=elapsed_ms(called),
                    )
                    records.append(("check_availability", arguments, output, True))
            text, validated_records, stock_clarification = validate_stock_answer(
                transcript,
                text,
                records,
                target,
                mock=backend.mode == "mock",
                clarification=clarification_for(target, catalog, tenant),
            )
            text = text.replace("**", "")  # The prompt forbids markdown; the model still bolds.
            added = [r for r in validated_records if r[0] == "add_to_cart" and r[3]]
            if not added and re.search(
                r"^(?:I(?:'ve| have)?\s+)?(?:added|put|placed)\b.{0,150}\b(?:cart|basket)\b",
                text,
                re.I,
            ):
                text = prefix + "I have not changed your cart."
            if added:
                # The cart confirmation is the gateway's own sentence from the tool results, so
                # what was added, the count and the total are never the model's words.
                prefix = "Fixture answer: " if backend.mode == "mock" else ""
                cart = merge_cart(conversation.cart, [r[2] for r in added])
                text = prefix + cart_answer([r[2] for r in added], cart)
                clarification = stock_clarification = False
            if foreign:
                # The shopper named an id this store does not own. Whatever the model looked up
                # or searched instead, the reply is the one fixed sentence and no substitute
                # cards or selection reach the screen. The catalog's denial, when the model did
                # call the tool, is already in the trace.
                prefix = "Fixture answer: " if backend.mode == "mock" else ""
                text = prefix + f"I could not find product id {foreign[0]} in this store."
                return Outcome(text, False, [], reference)
            if refused_cart and not added:
                text = cart_refusal or "I have not changed your cart."
                if backend.mode == "mock":
                    text = "Fixture answer: " + text
                clarification = bool(cart_refusal)
            if search_budget is not None and not any(
                name == "search_products" and allowed for name, _, _, allowed in validated_records
            ):
                # A model may answer a changed budget without refreshing the cards. Repeat
                # the prior search for a budget-only refinement, or use the new product words.
                query = transcript
                filters = {}
                if not any(
                    re.search(rf"\b{re.escape(word)}\b", transcript, re.I)
                    for word in CATEGORY_WORDS
                ):
                    query = conversation.preferences.get("last_query", transcript)
                    filters = {
                        key: conversation.preferences[key]
                        for key in ("category", "color", "size")
                        if key in conversation.preferences
                    }
                arguments = {**filters, "query": query[:200], "max_price_minor": search_budget}
                called = time.monotonic()
                output = execute_tool(tenant, "search_products", arguments, repository=catalog)
                trace.emit(
                    "tool.completed",
                    tool_name="search_products",
                    tool_call_id="gateway-search",
                    result_count=result_count("search_products", output),
                    catalog_version=catalog.catalog_version,
                    duration_ms=elapsed_ms(called),
                )
                validated_records.append(("search_products", arguments, output, True))
                products = output["products"]
                text = (
                    f"I found {len(products)} {'option' if len(products) == 1 else 'options'} "
                    "within your budget. Which would you like to see?"
                    if products
                    else "I found no matching items within that budget. Would you like to raise it?"
                )
                if backend.mode == "mock":
                    text = "Fixture answer: " + text
                clarification = stock_clarification = False
            outcome = Outcome(
                text, clarification or stock_clarification, validated_records, reference
            )
            if validated_records and all(
                name == "search_products" and allowed for name, _, _, allowed in validated_records
            ):
                products = validated_records[-1][2]["products"]
                if products:
                    names = [p["name"] for p in products]
                    listing = (
                        names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
                    )
                    budget_note = " within your budget" if search_budget is not None else ""
                    outcome.answer = (
                        prefix + f"I found the {listing}{budget_note}. Which one would you like?"
                    )
                    outcome.clarification = False
            return outcome

    async def agent(trace, stage, transcript):
        outcome = await respond(trace, stage, transcript)
        # A rejected/negated choice must not silently become the variant for a later "add it".
        if not re.search(
            r"\b(?:not|don't|don’t|do not|never|without|instead of)\b", transcript, re.I
        ):
            outcome.target = resolved["target"]
        return outcome

    def finish(result, transcript, outcome):
        result.update(
            transcript=transcript or "",
            input_source=message.input_source,
            catalog_version=catalog.catalog_version,
        )
        if result["status"] != "complete":
            # A failed turn shows the committed cards and changes nothing.
            result.update(
                product_cards=[catalog.card(tenant, p) for p in conversation.displayed],
                selected_product_id=conversation.selected_product_id,
                selected_variant_id=conversation.selected_variant_id,
                display_revision=conversation.display_revision,
                clarification=False,
                recommended_product_ids=[],
                stock_checks=[],
                cart=[dict(item) for item in conversation.cart],
            )
            return
        if outcome.preview:
            result["preview_image"] = outcome.preview["image"]
            result["preview"] = {k: v for k, v in outcome.preview.items() if k != "image"}
        displayed, revision, selected, variant, preferences = display_after(
            conversation, catalog, outcome
        )
        selection = conversation.selection if selected == conversation.selected_product_id else {}
        target = outcome.target
        if target and target.product_id == selected:
            selection = {
                "product_id": selected,
                "colors": sorted(target.colors),
                "sizes": sorted(target.sizes),
            }
        if outcome.clear_selection:
            selection = {}
        if any(name == "search_products" and allowed for name, _, _, allowed in outcome.records):
            selection = {}
        cart = merge_cart(
            conversation.cart,
            [
                output
                for name, _, output, allowed in outcome.records
                if allowed and name == "add_to_cart"
            ],
        )
        result.update(
            cart=cart,
            product_cards=[catalog.card(tenant, p) for p in displayed],
            selected_product_id=selected,
            # The variant committed with the selection (a verified check or an added line), so
            # a screen can keep its photo on turns without a stock check (N1).
            selected_variant_id=variant,
            display_revision=revision,
            clarification=outcome.clarification,
            recommended_product_ids=mentioned(outcome.answer, catalog, tenant, outcome),
            # Verified stock facts from this turn, so a screen can show them without prose.
            stock_checks=[
                output
                for name, _, output, allowed in outcome.records
                if allowed
                and name == "check_availability"
                and (target is None or target.matches(output))
            ],
            commit={
                "user": transcript,
                "assistant": outcome.answer,
                "preferences": preferences,
                "displayed": displayed,
                "display_revision": revision,
                "selected_product_id": selected,
                "selected_variant_id": variant,
                "clarification": outcome.answer if outcome.clarification else None,
                "selection": selection,
                "pending_action": outcome.pending_action,
                "preview": outcome.preview or conversation.preview,
                "cart": cart,
            },
        )

    return await _run(
        backend,
        session,
        deadline=deadline,
        on_event=on_event,
        permit=permit,
        audio=message.audio,
        text=message.text,
        source=message.input_source,
        agent=agent,
        finish=finish,
    )
