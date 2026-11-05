import hashlib
import json
import unittest
from unittest.mock import patch

import jsonschema

from retail_voice.admission import State
from retail_voice.api import fixture_media
from retail_voice.catalog import execute_tool
from retail_voice.catalog_repository import CatalogRepository, default_repository
from retail_voice.conversation import Conversation, Exchange, Reference, estimate_tokens
from retail_voice.errors import Denied, ToolArgumentError
from retail_voice.models.base import Decision
from retail_voice.models.mock import MockBackend
from retail_voice.pipeline import Message, Outcome, display_after, run_message
from retail_voice.prompts import SHOPPING_SYSTEM, local_time

SCHEMA = json.loads(
    (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "contracts/event.schema.json"
    ).read_text()
)
CATALOG = default_repository()
RAIN = "I need a lightweight jacket for rainy weather under 150 dollars"
RAIN_IDS = ["a-drizzle-anorak", "a-downpour-packable", "jacket-a"]
PINNED = 1793657700  # 2026-11-02 22:15 UTC: 3:15 PM in Denver, 5:15 PM in New York


class Shopper:
    """Drives run_message through the real admission state, so commits happen in finish()."""

    def __init__(self, backend=None, tenant="retailer-a"):
        self.state = State()
        self.session = self.state.reserve(tenant, "setup")
        self.session.conversation = Conversation(tenant, CATALOG.default_store(tenant))
        self.backend = backend or MockBackend()
        self.count = 0

    @property
    def conversation(self):
        return self.session.conversation

    async def say(self, text=None, audio=None, selected=None, revision="current", key=None):
        if key is None:
            key, self.count = f"k{self.count}", self.count + 1
        if revision == "current":
            revision = self.conversation.display_revision
        fingerprint = hashlib.sha256(json.dumps([text, selected, revision]).encode()).hexdigest()
        s, cached = self.state.begin(self.session.id, self.session.token, key, fingerprint)
        if cached is not None:
            return cached
        message = Message(
            audio=audio, text=text, selected_product_id=selected, display_revision=revision
        )
        result = await run_message(self.backend, s, message, CATALOG, deadline=10, permit=s.ticket)
        self.state.finish(s, result)
        return result


def kinds(result):
    return [e["event_type"] for e in result["events"]]


def ids(result):
    return [card["product_id"] for card in result["product_cards"]]


def tools_used(result, kind="tool.completed"):
    return [e["tool_name"] for e in result["events"] if e["event_type"] == kind]


class DisplayAfterTests(unittest.TestCase):
    """Selection rules after a turn, from the server's own state and allowed tool records."""

    def conversation(self, selected=None):
        c = Conversation("retailer-a", "store-a")
        c.displayed, c.display_revision, c.selected_product_id = tuple(RAIN_IDS), 1, selected
        return c

    def test_positional_reference_wins_over_a_stray_lookup(self):
        details = (
            "get_product_details",
            {"product_id": "a-drizzle-anorak"},
            {"product": {"product_id": "a-drizzle-anorak"}},
            True,
        )
        outcome = Outcome(
            "The Drizzle Anorak comes in green and navy.",
            records=[details],
            reference=Reference(product_id="a-downpour-packable", positional=True),
        )
        displayed, revision, selected, variant, _ = display_after(
            self.conversation(), CATALOG, outcome
        )
        self.assertEqual(
            (displayed, revision, selected, variant),
            (tuple(RAIN_IDS), 1, "a-downpour-packable", None),
        )

    def test_clicked_card_follows_the_one_product_a_search_reply_names(self):
        search = (
            "search_products",
            {"query": "warm jacket"},
            {"products": [{"product_id": p} for p in ("a-summit-down-jacket", *RAIN_IDS[:2])]},
            True,
        )
        outcome = Outcome(
            "The warmest jacket is the Summit Down Jacket at $229.",
            records=[search],
            reference=Reference(product_id="a-drizzle-anorak"),
        )
        displayed, revision, selected, _, _ = display_after(
            self.conversation("a-downpour-packable"), CATALOG, outcome
        )
        self.assertEqual(displayed, ("a-summit-down-jacket", *RAIN_IDS[:2]))
        self.assertEqual((revision, selected), (2, "a-summit-down-jacket"))

    def test_search_without_a_selection_selects_nothing(self):
        search = (
            "search_products",
            {"query": "rain"},
            {"products": [{"product_id": p} for p in RAIN_IDS]},
            True,
        )
        outcome = Outcome("I found 3 options from $69.", records=[search], reference=Reference())
        _, _, selected, _, _ = display_after(
            Conversation("retailer-a", "store-a"), CATALOG, outcome
        )
        self.assertIsNone(selected)
        # A search reply that singles out one of its results selects it: "a green jacket
        # at about $69" made the model search again and answer about the Drizzle Anorak.
        outcome = Outcome(
            "The Drizzle Anorak is $69 and comes in green.", records=[search], reference=Reference()
        )
        _, _, selected, _, _ = display_after(
            Conversation("retailer-a", "store-a"), CATALOG, outcome
        )
        self.assertEqual(selected, "a-drizzle-anorak")

    def test_reply_about_one_shown_product_selects_it_without_a_prior_selection(self):
        # "a green jacket at about $69" is not positional, but the model understood it
        # and answered about one shown card. That card becomes the selection, so the next
        # stock question has a target. A reply naming none or several still selects nothing.
        outcome = Outcome(
            "The Drizzle Anorak is $69 and comes in green or navy.", reference=Reference()
        )
        displayed, revision, selected, variant, _ = display_after(
            self.conversation(), CATALOG, outcome
        )
        self.assertEqual(
            (displayed, revision, selected, variant), (tuple(RAIN_IDS), 1, "a-drizzle-anorak", None)
        )
        for answer in (
            "Which color do you prefer?",
            "The Drizzle Anorak or the Downpour Packable Shell?",
        ):
            _, _, selected, _, _ = display_after(
                self.conversation(), CATALOG, Outcome(answer, reference=Reference())
            )
            self.assertIsNone(selected, answer)

    def test_spoken_position_stands_even_when_the_reply_names_another_product(self):
        outcome = Outcome(
            "The Drizzle Anorak is the lighter one.",
            reference=Reference(product_id="a-downpour-packable", positional=True),
        )
        _, _, selected, _, _ = display_after(self.conversation(), CATALOG, outcome)
        self.assertEqual(selected, "a-downpour-packable")

    def test_stale_selection_follows_the_one_product_the_reply_names(self):
        outcome = Outcome("The Ridgeline Rain Shell is the warmest at $129.", reference=Reference())
        _, _, selected, _, _ = display_after(
            self.conversation("a-drizzle-anorak"), CATALOG, outcome
        )
        self.assertEqual(selected, "jacket-a")

    def test_selection_stays_when_the_reply_names_several_or_none(self):
        for answer in (
            "The Ridgeline Rain Shell is warmer than the Drizzle Anorak.",
            "Sure, what size do you wear?",
        ):
            outcome = Outcome(answer, reference=Reference())
            _, _, selected, _, _ = display_after(
                self.conversation("a-drizzle-anorak"), CATALOG, outcome
            )
            self.assertEqual(selected, "a-drizzle-anorak", answer)


class MalformedToolArgumentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_argument_json_gets_a_hint_and_the_turn_completes(self):
        class Backend(MockBackend):
            def __init__(self):
                super().__init__()
                self.prompts = []

            async def decide(self, messages, tools, tool_choice="auto"):
                self.prompts.append(messages)
                if len(self.prompts) == 1:
                    return Decision(tool="get_product_details", arguments=None, call_id="c1")
                return Decision(answer="Fixture answer: the Drizzle Anorak is $69.00.")

        shopper = Shopper(Backend())
        r = await shopper.say("tell me about the drizzle anorak")
        self.assertEqual(r["status"], "complete")
        self.assertEqual(tools_used(r, "tool.denied"), ["get_product_details"])
        denied = next(e for e in r["events"] if e["event_type"] == "tool.denied")
        self.assertEqual(denied["reason"], "tool_arguments_denied")
        echoed, reply = shopper.backend.prompts[1][-2], shopper.backend.prompts[1][-1]
        self.assertEqual(json.loads(echoed["tool_calls"][0]["function"]["arguments"]), {})
        self.assertEqual(reply["role"], "tool")
        self.assertIn("invalid_arguments", reply["content"])
        for event in r["events"]:
            jsonschema.validate(event, SCHEMA)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_uses_no_tool(self):
        shopper = Shopper()
        r = await shopper.say("I need something for the weekend")
        self.assertEqual(r["status"], "complete")
        self.assertTrue(r["clarification"])
        self.assertEqual(kinds(r).count("model.completed"), 1)
        self.assertEqual(tools_used(r) + tools_used(r, "tool.denied"), [])
        self.assertEqual(r["product_cards"], [])
        self.assertEqual(shopper.conversation.pending_clarification, r["answer"])
        for event in r["events"]:
            jsonschema.validate(event, SCHEMA)

    async def test_search_details_availability_across_three_turns(self):
        shopper = Shopper()
        first = await shopper.say(RAIN)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(ids(first), RAIN_IDS)
        self.assertEqual(first["display_revision"], 1)
        self.assertEqual(tools_used(first), ["search_products"])
        search = next(e for e in first["events"] if e["event_type"] == "tool.completed")
        self.assertEqual(
            (search["result_count"], search["catalog_version"]), (3, CATALOG.catalog_version)
        )
        # The multi-result fixture names no product, so nothing is recommended or selected.
        self.assertEqual(
            first["recommended_product_ids"], [p["product_id"] for p in first["product_cards"]]
        )
        self.assertEqual(shopper.conversation.preferences["max_price_minor"], 15000)

        second = await shopper.say("Does the second one come in navy?")
        self.assertEqual(tools_used(second), ["get_product_details"])
        self.assertIn("Downpour Packable Shell comes in navy", second["answer"])
        self.assertEqual(ids(second), RAIN_IDS)
        self.assertEqual(second["display_revision"], 1)
        self.assertEqual(second["selected_product_id"], "a-downpour-packable")

        third = await shopper.say("Is medium available here?")
        self.assertEqual(tools_used(third), ["get_product_details", "check_availability"])
        self.assertIn("navy M: 4 in stock at Northline Downtown", third["answer"])
        self.assertEqual(shopper.conversation.selected_variant_id, "a-downpour-packable-navy-m")
        self.assertEqual(len(shopper.conversation.exchanges), 3)
        self.assertEqual(shopper.session.turn_count, 3)
        for result in (first, second, third):
            for event in result["events"]:
                jsonschema.validate(event, SCHEMA)
            self.assertEqual(result["events"][0]["input_source"], "text")
            self.assertNotIn("asr.completed", kinds(result))

    async def test_positions_resolve_against_the_committed_order(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        third = await shopper.say("Tell me about the third one")
        self.assertEqual(third["selected_product_id"], "jacket-a")
        self.assertIn("Ridgeline Rain Shell", third["answer"])
        option = await shopper.say("What colors does option 1 come in?")
        self.assertEqual(option["selected_product_id"], "a-drizzle-anorak")
        clicked = await shopper.say(
            "What colors does this come in?", selected="a-downpour-packable"
        )
        self.assertEqual(clicked["selected_product_id"], "a-downpour-packable")

    async def test_stale_or_ambiguous_positions_ask_without_a_model_call(self):
        empty = Shopper()
        r = await empty.say("Does the second one come in navy?")
        self.assertTrue(r["clarification"])
        self.assertIn("not shown any products", r["answer"])
        self.assertNotIn("model.completed", kinds(r))
        shopper = Shopper()
        await shopper.say(RAIN)
        stale = await shopper.say("Does the second one come in navy?", revision=0)
        self.assertIn("screen changed", stale["answer"])
        self.assertNotIn("model.completed", kinds(stale))
        self.assertEqual((ids(stale), stale["display_revision"]), (RAIN_IDS, 1))
        await shopper.say("Actually under 100")
        beyond = await shopper.say("Is the third one in stock?")
        self.assertIn("I showed 2 products", beyond["answer"])

    async def test_changed_budget_updates_results_and_keeps_context(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        cheaper = await shopper.say("Actually under 100")
        self.assertEqual(ids(cheaper), RAIN_IDS[:2])
        self.assertEqual(cheaper["display_revision"], 2)
        self.assertEqual(shopper.conversation.preferences["max_price_minor"], 10000)
        self.assertEqual(
            shopper.conversation.preferences["last_query"], "a lightweight jacket for rainy weather"
        )
        self.assertEqual(len(shopper.conversation.exchanges), 2)
        anything = await shopper.say("forget the budget, show me any rain jacket")
        self.assertTrue(anything["product_cards"])
        self.assertNotIn("max_price_minor", shopper.conversation.preferences)

    async def test_no_matches_and_zero_stock_are_reported_truthfully(self):
        shopper = Shopper()
        none = await shopper.say("rain jacket under 50 dollars")
        self.assertIn("no matches", none["answer"])
        self.assertEqual(none["product_cards"], [])
        # A new shopper: the first one keeps the $50 budget, as a model would.
        shopper = Shopper()
        found = await shopper.say("show me the stormline pro shell")
        self.assertEqual(ids(found)[0], "a-stormline-pro-shell")
        stock = await shopper.say("Is the first one available in red medium?")
        self.assertEqual(tools_used(stock), ["get_product_details", "check_availability"])
        self.assertIn("out of stock at Northline Downtown", stock["answer"])

    async def test_foreign_product_id_is_denied_by_the_repository_and_traced(self):
        shopper = Shopper()
        with patch.object(
            CatalogRepository, "details", autospec=True, side_effect=CATALOG.__class__.details
        ) as spy:
            r = await shopper.say("Check jacket-b at store-b")
        spy.assert_called_once_with(CATALOG, "retailer-a", "jacket-b")
        denied = [e for e in r["events"] if e["event_type"] == "tool.denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(
            (denied[0]["reason"], denied[0]["tool_name"]),
            ("catalog_scope_denied", "get_product_details"),
        )
        self.assertEqual(r["status"], "complete")
        self.assertIn("could not find", r["answer"])
        self.assertNotIn(
            "Harbor", json.dumps({k: v for k, v in r.items() if k not in {"events", "audio"}})
        )
        unknown = await Shopper().say("Check a-made-up-product please")
        same = [e for e in unknown["events"] if e["event_type"] == "tool.denied"][0]
        self.assertEqual(same["reason"], denied[0]["reason"])
        own = await Shopper(tenant="retailer-b").say("Check jacket-b at store-b")
        self.assertEqual(tools_used(own), ["get_product_details"])
        self.assertIn("Harbor Deck Jacket", own["answer"])

    async def test_model_cannot_change_tenant_or_store(self):
        class Forging(MockBackend):
            script = [
                Decision(
                    tool="search_products",
                    arguments={"query": "jacket", "tenant": "retailer-b"},
                    call_id="c1",
                ),
                Decision(
                    tool="check_availability",
                    arguments={
                        "product_id": "jacket-b",
                        "variant_id": "jacket-b-navy-m",
                        "store_id": "store-b",
                    },
                    call_id="c2",
                ),
                Decision(
                    tool="lookup_inventory",
                    arguments={"sku": "jacket-b", "store_id": "store-b"},
                    call_id="c3",
                ),
                Decision(answer="Fixture answer: done."),
            ]

            async def decide(self, messages, tools, tool_choice="auto"):
                return self.script.pop(0)

        shopper = Shopper(Forging())
        r = await shopper.say("find me a jacket")
        denied = [
            (e["tool_name"], e["reason"]) for e in r["events"] if e["event_type"] == "tool.denied"
        ]
        self.assertEqual(
            denied,
            [
                ("search_products", "tool_arguments_denied"),
                ("check_availability", "catalog_scope_denied"),
                ("lookup_inventory", "tool_scope_denied"),
            ],
        )
        self.assertEqual(r["product_cards"], [])
        self.assertEqual(
            (shopper.session.tenant, shopper.conversation.store_id), ("retailer-a", "store-a")
        )

    async def test_foreign_selected_product_never_reaches_the_model(self):
        seen = []

        class Recording(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                seen.append(json.dumps(messages))
                return await super().decide(messages, tools, tool_choice)

        shopper = Shopper(Recording())
        r = await shopper.say("tell me about this one", selected="jacket-b")
        self.assertIsNone(r["selected_product_id"])
        self.assertEqual(r["product_cards"], [])
        self.assertTrue(seen)
        self.assertFalse(any("jacket-b" in prompt for prompt in seen))

    async def test_failure_and_retry_keep_history_and_card_order(self):
        class Flaky(MockBackend):
            calls = 0
            broken = False

            async def decide(self, messages, tools, tool_choice="auto"):
                self.calls += 1
                if self.broken:
                    raise RuntimeError("sensitive backend detail")
                return await super().decide(messages, tools, tool_choice)

            async def synthesize(self, text):
                self.calls += 1
                if self.broken:
                    raise RuntimeError("sensitive backend detail")
                return await super().synthesize(text)

        backend = Flaky()
        shopper = Shopper(backend)
        await shopper.say(RAIN)
        backend.broken = True
        failed = await shopper.say("Does the second one come in navy?", key="retry-me")
        self.assertEqual((failed["status"], failed["reason"]), ("failed", "backend_failed"))
        self.assertNotIn("sensitive", json.dumps({k: v for k, v in failed.items() if k != "audio"}))
        self.assertEqual((ids(failed), failed["display_revision"]), (RAIN_IDS, 1))
        self.assertEqual(len(shopper.conversation.exchanges), 1)
        calls = backend.calls
        retried = await shopper.say("Does the second one come in navy?", key="retry-me")
        self.assertIs(retried, failed)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(shopper.session.turn_count, 2)
        backend.broken = False
        fixed = await shopper.say("Does the second one come in navy?", key="fixed")
        self.assertEqual(fixed["selected_product_id"], "a-downpour-packable")
        again = await shopper.say("Does the second one come in navy?", key="fixed")
        self.assertIs(again, fixed)
        self.assertEqual(len(shopper.conversation.exchanges), 2)

    async def test_tool_budget_exhaustion_fails_without_an_answer(self):
        class Searching(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                return Decision(
                    tool="search_products", arguments={"query": "jacket"}, call_id="loop"
                )

        shopper = Shopper(Searching())
        r = await shopper.say("find me a jacket")
        self.assertEqual((r["status"], r["reason"]), ("failed", "tool_budget_exhausted"))
        self.assertEqual(kinds(r).count("model.completed"), 5)
        self.assertEqual(kinds(r).count("tool.completed"), 4)
        self.assertNotIn("tts.completed", kinds(r))
        self.assertEqual(r["answer"], "")
        self.assertEqual(shopper.conversation.exchanges, [])

    async def test_long_history_is_trimmed_by_whole_exchanges(self):
        backend = MockBackend()
        backend.context_tokens = 6000
        shopper = Shopper(backend)
        padding = " ".join(["waterproof"] * 150)
        trimmed = []
        for _ in range(7):
            r = await shopper.say(f"rain jacket {padding}")
            self.assertEqual(r["status"], "complete")
            trimmed += [e for e in r["events"] if e["event_type"] == "context.trimmed"]
        self.assertTrue(trimmed)
        self.assertGreaterEqual(trimmed[-1]["dropped_exchanges"], 1)
        self.assertGreaterEqual(trimmed[-1]["kept_exchanges"], 1)
        for event in trimmed:
            jsonschema.validate(event, SCHEMA)
        self.assertEqual(len(shopper.conversation.exchanges), 7)

    async def test_message_that_cannot_fit_fails_visibly(self):
        backend = MockBackend()
        backend.context_tokens = 2048
        shopper = Shopper(backend)
        r = await shopper.say("rain jacket")
        self.assertEqual((r["status"], r["reason"]), ("failed", "context_limit"))
        self.assertNotIn("model.completed", kinds(r))
        self.assertEqual(r["events"][-1]["reason"], "context_limit")
        self.assertEqual(shopper.conversation.exchanges, [])

    async def test_events_never_carry_the_query_or_product_text(self):
        shopper = Shopper()
        with patch("retail_voice.prompts.now", lambda: PINNED):
            results = [
                await shopper.say(RAIN),
                await shopper.say("Does the second one come in navy?"),
                await shopper.say("What's the date today?"),
            ]
        self.assertIn("3:15 PM", results[2]["answer"])
        dump = json.dumps([e for r in results for e in r["events"]])
        # The budget would appear as a JSON number; a bare "15000" can also occur inside a
        # random hex id, which made this check flaky. The store clock stays out too.
        for text in (
            "lightweight",
            "rainy",
            "navy",
            "Drizzle",
            "Downpour",
            "query",
            ": 15000",
            "local_time",
            "November",
            "3:15",
            "America/Denver",
        ):
            self.assertNotIn(text, dump)

    async def test_audio_message_runs_asr_and_labels_the_source(self):
        shopper = Shopper()
        audio, _ = fixture_media()
        r = await shopper.say(audio=audio)
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["input_source"], "audio")
        self.assertIn("asr.completed", kinds(r))
        self.assertTrue(r["transcript"].startswith("Fixture transcript"))
        self.assertEqual(r["events"][0]["input_source"], "audio")


class SmallTalkTests(unittest.IsolatedAsyncioTestCase):
    """Greetings, the date or time and the weather need no tool and change no shopping state."""

    def setUp(self):
        clock = patch("retail_voice.prompts.now", lambda: PINNED)
        clock.start()
        self.addCleanup(clock.stop)

    async def test_small_talk_calls_no_tool_and_keeps_cards_and_selection(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        r = await shopper.say("Does the second one come in navy?")
        cards, selected, revision = ids(r), r["selected_product_id"], r["display_revision"]
        self.assertEqual(selected, "a-downpour-packable")
        cases = [
            ("Hi there", "hello, welcome to Northline Downtown."),
            ("Thank you!", "hello, welcome to Northline Downtown."),
            ("What's the date today?", "it is Monday, November 2, 2026, 3:15 PM"),
            ("What time is it?", "3:15 PM at the store (America/Denver)"),
            ("What's the weather like?", "I cannot check the weather here."),
            ("Is it going to rain tomorrow?", "I cannot check the weather here."),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                # The browser echoes the selected card with every message.
                r = await shopper.say(text, selected=selected)
                self.assertEqual(r["status"], "complete")
                self.assertIn(expected, r["answer"])
                self.assertEqual(tools_used(r) + tools_used(r, "tool.denied"), [])
                self.assertEqual(kinds(r).count("model.completed"), 1)
                self.assertEqual(ids(r), cards)
                self.assertEqual(r["selected_product_id"], selected)
                self.assertEqual(r["display_revision"], revision)
                self.assertFalse(r["clarification"])
                self.assertEqual(r["stock_checks"], [])
                self.assertEqual(r["recommended_product_ids"], [])
        self.assertEqual(shopper.conversation.displayed, tuple(cards))
        self.assertEqual(shopper.conversation.selected_product_id, selected)
        self.assertEqual(shopper.conversation.display_revision, revision)

    async def test_each_store_answers_in_its_own_time_zone(self):
        r = await Shopper(tenant="retailer-b").say("What time is it?")
        self.assertIn("5:15 PM at the store (America/New_York)", r["answer"])
        self.assertEqual(tools_used(r), [])

    async def test_a_greeting_or_weather_inside_a_request_still_shops(self):
        r = await Shopper().say("Hi, " + RAIN)
        self.assertEqual(tools_used(r), ["search_products"])
        self.assertEqual(ids(r), RAIN_IDS)

    def test_local_time_reads_like_a_person_says_it(self):
        self.assertEqual(local_time("America/Denver"), "Monday, November 2, 2026, 3:15 PM")
        self.assertEqual(local_time("America/New_York"), "Monday, November 2, 2026, 5:15 PM")
        midnight, noon = 1793602800, 1793646000
        self.assertEqual(
            local_time("America/Denver", midnight), "Monday, November 2, 2026, 12:00 AM"
        )
        self.assertEqual(local_time("America/Denver", noon), "Monday, November 2, 2026, 12:00 PM")
        self.assertEqual(
            local_time("America/Denver", noon - 3 * 3600 + 300), "Monday, November 2, 2026, 9:05 AM"
        )

    def test_small_talk_rules_come_first_and_keep_the_clarifying_rule(self):
        rules = SHOPPING_SYSTEM.split("Rules:")[1]
        marks = [
            "greeting or thanks",
            "date or the time",
            "what the weather is",
            "names no kind of product",
        ]
        spots = [rules.index(mark) for mark in marks]
        self.assertEqual(spots, sorted(spots))
        self.assertIn("I cannot check the weather here.", rules)
        self.assertIn("never describe a forecast", rules)
        self.assertIn("requested_product_ids lists the ids", rules)

    def test_reply_rule_limits_length_and_requires_grounded_details(self):
        """Speech replies use short sentences and facts returned by catalog tools."""
        rules = SHOPPING_SYSTEM.split("Rules:")[1]
        self.assertIn(
            "- Reply in English in at most two short sentences. Plain words, no lists, no "
            "markdown, no ids. Name the products. Explain one tradeoff when it helps, using only "
            "returned facts. Say so when stock is zero.",
            rules,
        )
        self.assertNotIn("30 words", rules)


class ToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def tool_replies(self, calls):
        seen = []

        class Scripted(MockBackend):
            script = [
                Decision(tool="check_availability", arguments=args, call_id=f"c{i}")
                for i, args in enumerate(calls)
            ] + [Decision(answer="Fixture answer: done.")]

            async def decide(self, messages, tools, tool_choice="auto"):
                seen.append(messages)
                return self.script.pop(0)

        shopper = Shopper(Scripted())
        r = await shopper.say("is medium in stock?")
        tool_messages = [m["content"] for m in seen[-1] if m["role"] == "tool"]
        return r, [json.loads(content) for content in tool_messages]

    async def test_own_product_errors_list_its_exact_variants(self):
        _, replies = await self.tool_replies(
            [
                {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-M"},
                {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-red-m"},
            ]
        )
        self.assertEqual(replies[0]["error"], "invalid_arguments")
        self.assertIn("a-downpour-packable-navy-m (navy M)", replies[0]["hint"])
        self.assertEqual(replies[1]["error"], "not_found")
        self.assertIn("a-downpour-packable-yellow-m (yellow M)", replies[1]["hint"])

    async def test_foreign_and_unknown_products_get_identical_bare_errors(self):
        _, replies = await self.tool_replies(
            [
                {"product_id": "jacket-b", "variant_id": "jacket-b-navy-m"},
                {"product_id": "no-such-product", "variant_id": "no-such-product-navy-m"},
                {"product_id": "jacket-b", "variant_id": "jacket-b-M"},
            ]
        )
        self.assertEqual(replies[0], {"error": "not_found"})
        self.assertEqual(replies[1], {"error": "not_found"})
        self.assertNotIn("jacket-b-navy-m", json.dumps(replies[2]))
        self.assertEqual(replies[2]["error"], "invalid_arguments")

    async def test_product_in_focus_carries_exact_variant_ids(self):
        seen = []

        class Recording(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                seen.append(messages)
                return await super().decide(messages, tools, tool_choice)

        shopper = Shopper(Recording())
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        await shopper.say("Tell me more about it.")
        current = [m for m in seen[-1] if m["role"] == "user"][-1]
        focus = json.loads(current["content"])["product_in_focus"]
        self.assertEqual(focus["product_id"], "a-downpour-packable")
        self.assertIn(
            {"variant_id": "a-downpour-packable-navy-m", "color": "navy", "size": "M"},
            focus["variants"],
        )
        first = json.loads(next(m for m in seen[0] if m["role"] == "user")["content"])
        self.assertIsNone(first["product_in_focus"])


class MemoryUnitTests(unittest.TestCase):
    def test_history_keeps_the_newest_whole_exchanges(self):
        conversation = Conversation("retailer-a", "store-a")
        for i in range(6):
            conversation.exchanges.append(
                Exchange(f"user {i} " + "x" * 70, f"reply {i} " + "y" * 70)
            )
        cost = conversation.exchanges[0].tokens()
        messages, dropped = conversation.history_for_model(cost * 2 + 1)
        self.assertEqual(dropped, 4)
        self.assertEqual(
            [m["content"][:6] for m in messages], ["user 4", "reply ", "user 5", "reply "]
        )
        self.assertEqual(conversation.history_for_model(0), ([], 6))
        self.assertEqual(estimate_tokens("x" * 35), 10 + 12)
        self.assertEqual(estimate_tokens("é" * 10), 20 + 12)
        self.assertEqual(estimate_tokens("ab" + "雨" * 3), 7 + 12)

    def test_commit_applies_explicit_values_once(self):
        conversation = Conversation("retailer-a", "store-a")
        conversation.commit(
            {
                "user": "u",
                "assistant": "a",
                "preferences": {"max_price_minor": 15000},
                "displayed": ("a", "b"),
                "display_revision": 1,
                "selected_product_id": "b",
                "selected_variant_id": None,
                "clarification": None,
            }
        )
        self.assertEqual((conversation.displayed, conversation.display_revision), (("a", "b"), 1))
        self.assertEqual(conversation.preferences, {"max_price_minor": 15000})


class DispatcherTests(unittest.TestCase):
    def call(self, name, arguments, tenant="retailer-a"):
        return execute_tool(tenant, name, arguments)

    def test_search_arguments_are_validated_and_normalized(self):
        result = self.call(
            "search_products",
            {"query": "jacket", "size": "medium", "color": "Grey", "category": None},
        )
        self.assertEqual(result["applied_filters"], {"size": "M", "color": "gray"})
        for arguments in (
            {"query": "x" * 201},
            {"query": "jacket", "limit": 4},
            {"query": "jacket", "limit": True},
            {"query": "jacket", "max_price_minor": "15000"},
            {"query": "jacket", "max_price_minor": 150.5},
            {"query": "jacket", "max_price_minor": -1},
            {"query": "jacket", "category": "coats"},
            {"query": "jacket", "tenant": "retailer-b"},
            {"category": "jackets"},
            ["jacket"],
        ):
            with self.assertRaises(ToolArgumentError, msg=arguments):
                self.call("search_products", arguments)

    def test_zero_budget_means_no_ceiling(self):
        result = self.call(
            "search_products", {"query": "stormline pro shell", "max_price_minor": 0}
        )
        self.assertNotIn("max_price_minor", result["applied_filters"])
        self.assertEqual(result["products"][0]["product_id"], "a-stormline-pro-shell")

    def test_unknown_tool_and_bad_ids(self):
        with self.assertRaises(Denied) as caught:
            self.call("shell", {"command": "ls"})
        self.assertEqual(str(caught.exception), "tool_scope_denied")
        with self.assertRaises(ToolArgumentError):
            self.call("get_product_details", {"product_id": "../catalog.json"})
        for product_id in ("jacket-b", "no-such-product"):
            with self.assertRaises(Denied) as caught:
                self.call("get_product_details", {"product_id": product_id})
            self.assertEqual(str(caught.exception), "catalog_scope_denied")

    def test_store_must_be_one_of_the_tenant_stores(self):
        ok = self.call(
            "check_availability",
            {
                "product_id": "jacket-a",
                "variant_id": "jacket-a-navy-m",
                "store_id": "store-a-riverside",
            },
        )
        self.assertEqual(ok["store_id"], "store-a-riverside")
        with self.assertRaises(Denied):
            self.call(
                "check_availability",
                {"product_id": "jacket-a", "variant_id": "jacket-a-navy-m", "store_id": "store-b"},
            )
        default = self.call(
            "check_availability", {"product_id": "jacket-a", "variant_id": "jacket-a-navy-m"}
        )
        self.assertEqual((default["store_id"], default["quantity"]), ("store-a", 3))
