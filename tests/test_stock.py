"""Stock speech must be tied to the shopper's target, even when the model is wrong."""

import json
import unittest

from test_conversation import CATALOG, RAIN, Shopper

from retail_voice.cart import asks_to_change_cart
from retail_voice.models.base import Decision
from retail_voice.models.mock import MockBackend
from retail_voice.speech_text import spoken
from retail_voice.stock import named_product_ids, stock_answer, words

HARBOR = "Is the Harbor Deck Jacket available in red, size medium, at this store?"
EXACT = {"product_id": "jacket-b", "variant_id": "jacket-b-red-m", "store_id": "store-b"}


class StockAnswerTests(unittest.IsolatedAsyncioTestCase):
    def test_contractions_do_not_choose_sizes(self):
        choices = {"s": "S", "m": "M", "medium": "M"}
        self.assertEqual(words("Yes, that's the one I'm looking for", choices), set())
        self.assertEqual(words("Yes, that’s the one I’m looking for", choices), set())
        self.assertEqual(words("Size M please", choices), {"M"})

    async def run_script(
        self, question, arguments=None, answer="Yes, it is in stock.", shopper=None
    ):
        class Scripted(MockBackend):
            synthesized = None

            def __init__(self):
                self.script = (
                    [Decision(tool="check_availability", arguments=arguments)]
                    if arguments is not None
                    else []
                ) + [Decision(answer=answer)]

            async def decide(self, messages, tools, tool_choice="auto"):
                return self.script.pop(0)

            async def synthesize(self, text):
                self.synthesized = text
                return await super().synthesize(text)

        shopper = shopper or Shopper(tenant="retailer-b")
        shopper.backend = Scripted()
        result = await shopper.say(question)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(shopper.backend.synthesized, spoken(result["answer"]))
        return result, shopper

    async def test_color_question_cannot_set_a_model_guessed_size(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        seen = []

        class Guessing(MockBackend):
            def __init__(self):
                self.script = [
                    Decision(
                        tool="check_availability",
                        arguments={
                            "product_id": "a-downpour-packable",
                            "variant_id": "a-downpour-packable-navy-m",
                        },
                    ),
                    Decision(
                        tool="get_product_details",
                        arguments={
                            "product_id": "a-downpour-packable",
                        },
                    ),
                    Decision(answer="Yes, the Downpour Packable Shell comes in navy."),
                ]

            async def decide(self, messages, tools, tool_choice="auto"):
                if messages[-1]["role"] == "tool":
                    seen.append(json.loads(messages[-1]["content"]))
                return self.script.pop(0)

        shopper.backend = Guessing()
        result = await shopper.say("Tell me about the second one in navy.")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(seen[0]["error"], "variant_not_selected")
        self.assertEqual(result["stock_checks"], [])
        self.assertIsNone(result["selected_variant_id"])
        self.assertEqual(result["selected_product_id"], "a-downpour-packable")
        self.assertNotIn("medium", result["answer"].lower())
        self.assertEqual(
            [e["tool_name"] for e in result["events"] if e["event_type"] == "tool.completed"],
            ["get_product_details"],
        )

    async def test_zero_stock_overrides_incorrect_model_prose_before_tts(self):
        result, shopper = await self.run_script(HARBOR, EXACT)
        fact = CATALOG.availability("retailer-b", **EXACT)
        self.assertEqual(result["answer"], "Fixture answer: " + stock_answer(fact))
        self.assertEqual(result["stock_checks"], [fact])
        self.assertEqual(result["selected_product_id"], "jacket-b")
        self.assertEqual(shopper.conversation.selected_variant_id, "jacket-b-red-m")
        self.assertFalse(result["clarification"])

    async def test_wrong_or_absent_lookup_is_replaced_by_the_gateways_own_exact_check(self):
        """the gateway resolved the exact product, variant and store, so a wrong lookup
        or none at all gets the gateway's own fresh check, never the model's claim."""
        for arguments in (
            {"product_id": "b-squall-rain-shell", "variant_id": "b-squall-rain-shell-red-m"},
            EXACT | {"variant_id": "jacket-b-navy-m"},
            EXACT | {"variant_id": "jacket-b-red-l"},
            EXACT | {"store_id": "store-a"},
            None,
        ):
            with self.subTest(arguments=arguments):
                result, shopper = await self.run_script(HARBOR, arguments)
                self.assertFalse(result["clarification"])
                self.assertRegex(
                    result["answer"],
                    r"^Fixture answer: Harbor Deck Jacket in red M (is out of stock|: \d+ in stock) at Harbor Supply Wharf\.$",
                )
                self.assertEqual(
                    [(c["variant_id"], c["store_id"]) for c in result["stock_checks"]],
                    [("jacket-b-red-m", "store-b")],
                )
                self.assertEqual([c["product_id"] for c in result["product_cards"]], ["jacket-b"])
                self.assertEqual(shopper.conversation.selected_variant_id, "jacket-b-red-m")
                self.assertNotIn("Squall", result["answer"])
                completed = [e for e in result["events"] if e["event_type"] == "tool.completed"]
                self.assertEqual(completed[-1]["tool_call_id"], "gateway-stock")
                self.assertEqual(completed[-1]["tool_name"], "check_availability")

    async def test_ambiguous_or_unsupported_target_does_not_adopt_model_choice(self):
        for text in (
            "Is the Harbor Deck Jacket available in medium?",
            "Is the Harbor Deck Jacket available in red or navy medium?",
            "Is the Harbor Deck Jacket available in purple medium?",
            "Is the Harbor Deck Jacket available in red medium at Airport?",
            "Is the Harbor Deck Jacket or Squall Rain Shell available in red medium?",
        ):
            with self.subTest(text=text):
                result, _ = await self.run_script(text, EXACT)
                self.assertTrue(result["clarification"])
                self.assertEqual(result["stock_checks"], [])
        # Another retailer's id is not a clarification but the fixed not-found reply.
        result, _ = await self.run_script("Is jacket-a available in red medium?", EXACT)
        self.assertEqual(
            result["answer"], "Fixture answer: I could not find product id jacket-a in this store."
        )
        self.assertEqual(result["stock_checks"], [])

    async def test_prior_selection_cannot_override_a_new_named_product(self):
        shopper = Shopper(tenant="retailer-b")
        shopper.conversation.selected_product_id = "b-squall-rain-shell"
        result, _ = await self.run_script(HARBOR, EXACT, shopper=shopper)
        self.assertEqual(result["selected_product_id"], "jacket-b")
        self.assertEqual(result["stock_checks"][0]["quantity"], 0)

    async def test_color_two_turns_back_still_targets_the_selected_product(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        await shopper.say("Yes, that's the one I'm looking for.")
        result, _ = await self.run_script(
            "Is medium available here?",
            {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-navy-m"},
            shopper=shopper,
        )
        self.assertFalse(result["clarification"])
        self.assertEqual(result["stock_checks"][0]["variant_id"], "a-downpour-packable-navy-m")

    async def test_followup_uses_the_previous_color_for_the_selected_product(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        result, _ = await self.run_script(
            "Do they have medium at this store?",
            {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-navy-m"},
            shopper=shopper,
        )
        self.assertFalse(result["clarification"])
        self.assertEqual(result["stock_checks"][0]["quantity"], 4)

    async def test_budget_change_is_not_a_stock_turn(self):
        """The model only sees the resolved target for a stock question. A volunteered lookup on
        a budget change is dropped, not spoken, and the reply keeps its words."""
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        seen = []

        class Scripted(MockBackend):
            def __init__(self):
                self.script = [
                    Decision(
                        tool="check_availability",
                        arguments={
                            "product_id": "a-downpour-packable",
                            "variant_id": "a-downpour-packable-navy-m",
                        },
                    ),
                    Decision(answer="Fixture answer: Under 100 dollars, the Drizzle Anorak."),
                ]

            async def decide(self, messages, tools, tool_choice="auto"):
                seen.append(messages[-1]["content"])
                return self.script.pop(0)

        shopper.backend = Scripted()
        result = await shopper.say("Actually, I want to stay under 100 dollars.")
        self.assertNotIn("availability_target", seen[0])
        self.assertIn("within your budget", result["answer"])
        self.assertTrue(all(p["price_minor"] <= 10000 for p in result["product_cards"]))
        self.assertFalse(result["clarification"])
        self.assertEqual(result["stock_checks"], [])
        self.assertEqual(shopper.conversation.selected_variant_id, None)

    async def test_volunteered_check_of_the_selected_product_backs_a_stock_claim(self):
        """ "Yes, that's the one" is not a stock question. On the reference GPU host the model still checked
        the selected product and said it was in stock, and the claim was replaced by the stock
        clarification. Only an already shopper-resolved variant can back the claim; a check of
        some other product does not back it."""
        for checked, expected, confirmed in (
            (
                "a-downpour-packable",
                "Fixture answer: Downpour Packable Shell in navy M: 4 in stock at "
                "Northline Downtown.",
                True,
            ),
            ("a-drizzle-anorak", None, False),
        ):
            shopper = Shopper()
            await shopper.say(RAIN)
            await shopper.say("Does the second one come in navy?")
            await shopper.say("Is medium available here?")

            class Scripted(MockBackend):
                def __init__(self):
                    self.script = [
                        Decision(
                            tool="check_availability",
                            arguments={
                                "product_id": checked,
                                "variant_id": f"{checked}-navy-m",
                            },
                        ),
                        Decision(answer="Fixture answer: Great choice, it is in stock here."),
                    ]

                async def decide(self, messages, tools, tool_choice="auto"):
                    return self.script.pop(0)

            shopper.backend = Scripted()
            result = await shopper.say("Yes, that's the one I'm looking for.")
            self.assertEqual(result["selected_product_id"], "a-downpour-packable")
            if confirmed:
                self.assertEqual(result["answer"], expected)
                self.assertFalse(result["clarification"])
                self.assertEqual(
                    [c["variant_id"] for c in result["stock_checks"]],
                    ["a-downpour-packable-navy-m"],
                )
            else:
                self.assertIn("could not confirm stock", result["answer"])
                self.assertTrue(result["clarification"])
                self.assertEqual(result["stock_checks"], [])

    async def test_a_color_or_size_the_product_lacks_gets_a_specific_clarification(self):
        """Spoken color and size regression: "do you have that in blue and in size medium" was answered
        with a form to fill in. The gateway now says what the catalog offers and asks only for
        what is missing; the next turn with an offered color resolves and the check stands."""

        class Scripted(MockBackend):
            def __init__(self, script):
                self.script = script

            async def decide(self, messages, tools, tool_choice="auto"):
                return self.script.pop(0)

        check = Decision(
            tool="check_availability",
            arguments={
                "product_id": "a-downpour-packable",
                "variant_id": "a-downpour-packable-navy-m",
            },
        )
        for question, expected in (
            (
                "Okay, do you have the second one in blue and in size medium?",
                "Fixture answer: The Downpour Packable Shell doesn't come in blue. In size M it "
                "comes in yellow and navy. Which color should I check?",
            ),
            (
                "Do you have the second one in navy here?",
                "Fixture answer: Which size should I check in navy? It comes in M and L.",
            ),
            (
                "Do you have the second one in navy, size small?",
                "Fixture answer: The Downpour Packable Shell in navy comes in M and L, not S. "
                "Which size should I check?",
            ),
        ):
            shopper = Shopper()
            await shopper.say(RAIN)
            shopper.backend = Scripted([check, Decision(answer="Yes, navy medium is in stock.")])
            result = await shopper.say(question)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["answer"], expected)
            self.assertTrue(result["clarification"])
            self.assertEqual(result["stock_checks"], [])
            self.assertEqual(result["selected_product_id"], "a-downpour-packable")
        shopper.backend = Scripted([check, Decision(answer="Yes, navy medium is in stock.")])
        result = await shopper.say("Well, in navy size medium")
        self.assertEqual(
            result["answer"],
            "Fixture answer: Downpour Packable Shell in navy M: 4 in stock at Northline Downtown.",
        )
        self.assertEqual(
            [c["variant_id"] for c in result["stock_checks"]], ["a-downpour-packable-navy-m"]
        )

    async def test_add_to_cart_after_a_verified_check_commits_the_cart(self):
        """the cart lives in session memory, the confirmation is the gateway's sentence,
        and the model answers "what's in my cart" from the payload without a tool."""
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        await shopper.say("Is medium available here?")
        result = await shopper.say("Great, add it to the cart.")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["answer"],
            "Fixture answer: Added the Downpour Packable Shell in navy M, $89.00, to your "
            "cart. You have 1 item totaling $89.00.",
        )
        self.assertFalse(result["clarification"])
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in result["cart"]],
            [("a-downpour-packable-navy-m", 1)],
        )
        self.assertEqual(
            [e["tool_name"] for e in result["events"] if e["event_type"] == "tool.completed"],
            ["add_to_cart"],
        )
        self.assertEqual(shopper.conversation.cart[0]["price_minor"], 8900)
        self.assertEqual(shopper.conversation.selected_variant_id, "a-downpour-packable-navy-m")
        # Events name the tool, never the line item.
        self.assertNotIn("Downpour", json.dumps(result["events"]))

        listed = await shopper.say("What's in my cart?")
        self.assertIn("Downpour Packable Shell in navy M x1", listed["answer"])
        self.assertEqual([e for e in listed["events"] if e["event_type"].startswith("tool")], [])
        again = await shopper.say("Add it to the cart.")
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in again["cart"]],
            [("a-downpour-packable-navy-m", 2)],
        )
        self.assertIn("You have 2 items totaling $178.00", again["answer"])
        pay = await shopper.say("Can I check out now?")
        self.assertIn("no checkout", pay["answer"])
        self.assertEqual(len(pay["cart"]), 1)
        self.assertEqual(shopper.conversation.cart[0]["quantity"], 2)

    async def add_script(self, shopper, *calls, text="Add it to the cart."):
        """One turn whose model calls add_to_cart with each argument set, then answers."""

        class Scripted(MockBackend):
            seen: list = []

            def __init__(self):
                self.script = [Decision(tool="add_to_cart", arguments=a) for a in calls] + [
                    Decision(answer="Fixture answer: done.")
                ]

            async def decide(self, messages, tools, tool_choice="auto"):
                if messages[-1]["role"] == "tool":
                    Scripted.seen.append(json.loads(messages[-1]["content"]))
                return self.script.pop(0)

        backend, shopper.backend = shopper.backend, Scripted()
        try:
            result = await shopper.say(text)
        finally:
            shopper.backend = backend
        self.assertEqual(result["status"], "complete")
        return result, Scripted.seen

    async def settled_navy_m(self):
        """Northline with the Downpour Packable Shell in navy M selected and checked (4 left)."""
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        result = await shopper.say("Is medium available here?")
        self.assertEqual(result["stock_checks"][0]["quantity"], 4)
        return shopper

    async def test_short_quantity_is_not_called_out_of_stock(self):
        """4 in stock and 5 asked is a short quantity, not zero stock."""
        shopper = await self.settled_navy_m()
        result, seen = await self.add_script(
            shopper,
            {
                "product_id": "a-downpour-packable",
                "variant_id": "a-downpour-packable-navy-m",
                "quantity": 5,
            },
            text="Add five of them to the cart.",
        )
        self.assertEqual(
            result["answer"],
            "Fixture answer: Only 4 of the Downpour Packable Shell in navy M are in stock at "
            "Northline Downtown, so I did not add 5.",
        )
        self.assertEqual(result["cart"], [])
        self.assertEqual(shopper.conversation.cart, [])
        # The gateway executes the checked cart action without a model round trip.
        self.assertEqual(seen, [])
        self.assertEqual(
            len([e for e in result["events"] if e.get("tool_call_id") == "gateway-cart"]), 1
        )
        self.assertNotIn("out of stock", result["answer"])

    async def test_repeated_adds_never_exceed_the_stock(self):
        """five single adds of a variant with 4 in stock stop at 4 in the cart."""
        shopper = await self.settled_navy_m()
        for count in range(1, 5):
            result = await shopper.say("Add it to the cart.")
            self.assertEqual(
                [(i["variant_id"], i["quantity"]) for i in result["cart"]],
                [("a-downpour-packable-navy-m", count)],
            )
        result = await shopper.say("Add it to the cart.")
        self.assertEqual(
            result["answer"],
            "Fixture answer: You already have 4 in your cart and only 4 are in stock.",
        )
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in result["cart"]],
            [("a-downpour-packable-navy-m", 4)],
        )
        self.assertEqual(shopper.conversation.cart[0]["quantity"], 4)
        self.assertFalse(result["clarification"])

    async def test_one_resolved_cart_action_cannot_repeat_model_additions(self):
        """A model wanting two adds cannot override the shopper's single requested quantity."""
        shopper = await self.settled_navy_m()
        variant = {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-navy-m"}
        result, seen = await self.add_script(
            shopper, variant | {"quantity": 3}, variant | {"quantity": 2}, text="Add three more."
        )
        self.assertEqual(seen, [])
        self.assertEqual(
            len([e for e in result["events"] if e.get("tool_call_id") == "gateway-cart"]), 1
        )
        self.assertEqual(
            result["answer"],
            "Fixture answer: Added 3 of the Downpour Packable Shell in navy M, $89.00 each, to "
            "your cart. You have 3 items totaling $267.00.",
        )
        self.assertEqual([i["quantity"] for i in shopper.conversation.cart], [3])

    async def test_result_carries_the_committed_variant_through_small_talk(self):
        """the focus photo needs the variant the gateway settled on, also on a turn
        without a stock check, so the result carries the committed selected_variant_id."""
        shopper = await self.settled_navy_m()
        self.assertEqual(shopper.conversation.selected_variant_id, "a-downpour-packable-navy-m")
        result = await shopper.say("What's the date today?")
        self.assertEqual(result["stock_checks"], [])
        self.assertEqual(result["selected_product_id"], "a-downpour-packable")
        self.assertEqual(result["selected_variant_id"], "a-downpour-packable-navy-m")
        # A new search without the product clears both.
        result = await shopper.say("I need a warm hat under 40 dollars")
        self.assertIsNone(result["selected_product_id"])
        self.assertIsNone(result["selected_variant_id"])

    async def test_failed_turn_returns_the_committed_variant(self):
        shopper = await self.settled_navy_m()

        class Broken(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                raise RuntimeError("model down")

        shopper.backend = Broken()
        result = await shopper.say("Tell me more about it.")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["selected_variant_id"], "a-downpour-packable-navy-m")

    async def test_out_of_stock_variant_is_not_added(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say(
            "Forget the budget. Is the Stormline Pro Shell available in red, size medium, "
            "at this store?"
        )
        result = await shopper.say("Add it to the cart.")
        self.assertEqual(
            result["answer"],
            "Fixture answer: The Stormline Pro Shell in red M is out of stock at Northline "
            "Downtown, so I did not add it. Would you like another size or item?",
        )
        self.assertEqual(result["cart"], [])
        self.assertEqual(shopper.conversation.cart, [])

    async def test_stock_question_shows_the_model_the_resolved_target(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        seen = []
        backend = shopper.backend
        original = backend.decide

        async def decide(messages, tools, tool_choice="auto"):
            seen.append(messages[-1]["content"])
            return await original(messages, tools, tool_choice)

        backend.decide = decide
        result = await shopper.say("Is medium available here?")
        self.assertIn('"availability_target": {"product_id": "a-downpour-packable"', seen[0])
        self.assertEqual(result["stock_checks"][0]["quantity"], 4)

    async def test_no_match_reply_without_a_search_is_verified(self):
        answer = "Fixture answer: We do not carry any rain jackets under 50 dollars."
        result, _ = await self.run_script(
            "Do you have any rain jackets under 50 dollars?", answer=answer
        )
        self.assertIn("no matching items", result["answer"])
        self.assertEqual(result["product_cards"], [])
        self.assertTrue(any(e.get("tool_call_id") == "gateway-search" for e in result["events"]))
        self.assertFalse(result["clarification"])

    async def test_foreign_or_unknown_product_id_gets_one_fixed_reply(self):
        """Whatever the model searches or says, an id this store does not own is not found."""
        shopper = Shopper()  # Northline
        await shopper.say(RAIN)
        for text, token in (
            ("What is the price of product jacket-b?", "jacket-b"),
            ("Look up product id jacket-b and tell me its price.", "jacket-b"),
            ("Is jacket-b-navy-m in stock here?", "jacket-b-navy-m"),
            ("Tell me about jacket-a-no-such-variant.", "jacket-a-no-such-variant"),
            ("Tell me about a-no-such-thing please.", "a-no-such-thing"),
            # a numeric suffix stays part of the id, so it is unknown, not jacket-a.
            ("Tell me about jacket-a-123.", "jacket-a-123"),
            ("Is jacket-a-123 in stock here?", "jacket-a-123"),
            ("Tell me about a-downpour-packable-2.", "a-downpour-packable-2"),
            # Speech recognition's spelling of the same request, as heard on a browser microphone.
            ("Look up product ID jacket B and tell me its price.", "jacket-b"),
            ("What is the price of product jacket b?", "jacket-b"),
        ):
            with self.subTest(text=text):
                result, _ = await self.run_script(
                    text, answer=f"Fixture answer: {token} costs $129.00.", shopper=shopper
                )
                self.assertEqual(
                    result["answer"],
                    f"Fixture answer: I could not find product id {token} in this store.",
                )
                self.assertEqual(result["stock_checks"], [])
                self.assertFalse(result["clarification"])
                self.assertEqual(
                    [c["product_id"] for c in result["product_cards"]],
                    ["a-drizzle-anorak", "a-downpour-packable", "jacket-a"],
                )

    async def test_own_product_or_variant_id_is_answered_normally(self):
        shopper = Shopper()
        answer = "Fixture answer: the Downpour Packable Shell comes in navy and yellow."
        for text in (
            "Tell me about a-downpour-packable.",
            "Does a-downpour-packable-navy-m come in other colors?",
            "What colors does jacket-a come in?",
        ):
            with self.subTest(text=text):
                result, _ = await self.run_script(text, answer=answer, shopper=shopper)
                self.assertEqual(result["answer"], answer)

    async def test_typed_and_spoken_ids_resolve_the_same_stock_target(self):
        """Spoken product IDs resolve to the same catalog identity as typed IDs."""
        fact = CATALOG.availability(
            "retailer-a", product_id="jacket-a", variant_id="jacket-a-navy-m", store_id="store-a"
        )
        for text in (
            "Is jacket-a available in navy medium at this store?",
            "Is product ID jacket A available in navy medium at this store?",
        ):
            with self.subTest(text=text):
                result, _ = await self.run_script(
                    text,
                    {"product_id": "jacket-a", "variant_id": "jacket-a-navy-m"},
                    shopper=Shopper(),
                )
                self.assertEqual(result["answer"], "Fixture answer: " + stock_answer(fact))
                self.assertEqual(result["stock_checks"], [fact])

    async def test_variant_id_fixes_the_stock_target_and_contradictions_clarify(self):
        """An owned variant ID selects that variant; contradictory preferences need clarification."""
        fact = CATALOG.availability(
            "retailer-a",
            product_id="a-downpour-packable",
            variant_id="a-downpour-packable-navy-m",
            store_id="store-a",
        )
        arguments = {
            "product_id": "a-downpour-packable",
            "variant_id": "a-downpour-packable-navy-m",
        }
        result, _ = await self.run_script(
            "Is a-downpour-packable-navy-m in stock here?", arguments, shopper=Shopper()
        )
        self.assertEqual(result["stock_checks"], [fact])
        result, _ = await self.run_script(
            "Is a-downpour-packable-navy-m available in yellow?", arguments, shopper=Shopper()
        )
        self.assertTrue(result["clarification"])
        self.assertEqual(result["stock_checks"], [])

    async def test_owned_variant_id_reaches_parent_details_through_the_tool_path(self):
        """The model is shown the product id, so the real dispatcher
        completes get_product_details instead of rejecting a variant id."""
        shopper = Shopper()
        result = await shopper.say("Tell me about a-downpour-packable-navy-m.")
        completed = [
            e
            for e in result["events"]
            if e["event_type"] == "tool.completed" and e.get("tool_name") == "get_product_details"
        ]
        self.assertEqual(len(completed), 1)
        self.assertNotIn("could not find", result["answer"])
        self.assertEqual(result["selected_product_id"], "a-downpour-packable")

    async def test_a_spoken_jacket_a_is_not_an_id_without_product_or_id_before_it(self):
        """Harbor does not own jacket-a; ordinary speech must not trip the guard."""
        answer = "Fixture answer: the Harbor Deck Jacket comes in medium and large."
        result, _ = await self.run_script(
            "I need a jacket a size larger than medium.", answer=answer
        )
        self.assertEqual(result["answer"], answer)

    async def test_markdown_bold_is_stripped_from_the_reply(self):
        result, _ = await self.run_script(
            "Tell me about the Harbor Deck Jacket", answer="Fixture answer: the **Deck Jacket**."
        )
        self.assertEqual(result["answer"], "Fixture answer: the Deck Jacket.")

    async def test_unsolicited_stock_assertion_without_a_check_is_replaced(self):
        result, _ = await self.run_script(
            "Tell me about the Harbor Deck Jacket", answer="We have 3 left."
        )
        self.assertTrue(result["clarification"])
        self.assertEqual(result["stock_checks"], [])

    async def test_ordinary_product_reply_is_preserved(self):
        answer = "Fixture answer: the Harbor Deck Jacket comes in navy and red."
        result, _ = await self.run_script("Tell me about the Harbor Deck Jacket", answer=answer)
        self.assertEqual(result["answer"], answer)
        self.assertFalse(result["clarification"])
        self.assertNotIn("shopper_message", json.dumps(result["events"]))


class IdTokenTests(unittest.TestCase):
    """The id reader that the selection guard, the stock target and the browser mirror share."""

    def test_whole_dashed_tokens_with_digits_stay_whole(self):
        for text, expected in (
            ("Tell me about jacket-a-123.", ["jacket-a-123"]),
            ("Is jacket-a-123-navy-m here?", ["jacket-a-123-navy-m"]),
            ("a-downpour-packable-2 please", ["a-downpour-packable-2"]),
            ("jacket-a", ["jacket-a"]),
            ("Look up product id jacket-b and tell me its price.", ["jacket-b"]),
            ("Look up product ID jacket B and tell me its price.", ["jacket-b"]),
            ("Is a-downpour-packable-navy-m in stock?", ["a-downpour-packable-navy-m"]),
            ("I need a jacket a size larger than medium.", []),
            ("I need a 2-in-1 jacket under 150 dollars.", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(named_product_ids(text), expected)


class CartRequestTests(unittest.IsolatedAsyncioTestCase):
    """The gateway refuses cart changes unless the shopper requested an addition."""

    def test_add_requests_are_recognized_and_other_turns_are_not(self):
        for text in (
            "Great, add it to the cart.",
            "Add it to the cart.",
            "Add five of them to the cart.",
            "I'll take it.",
            "Put it in my cart please",
            "Can I buy this one?",
            "Add the navy medium.",
            "I'll have it.",
            "I'd like that one.",
            "I\u2019ll go with the navy one.",
            "Get me the medium.",
            "Grab me two of those.",
            "Ring it up.",
            "I want it in large.",
            "Order two for me",
        ):
            with self.subTest(text=text):
                self.assertTrue(asks_to_change_cart(text))
        for text in (
            "Actually, I want to stay under 100 dollars.",
            "I'd like a rain jacket under 150 dollars.",
            "I would like to see the warmest jacket.",
            "Yes please.",
            "Sure, go ahead.",
            "Is medium available here?",
            "Does the second one come in navy?",
            "What's the date today?",
            "Hi there",
            "Do you have any rain jackets under 50 dollars?",
            "Forget the budget. Is the Stormline Pro Shell available in red, size medium?",
        ):
            with self.subTest(text=text):
                self.assertFalse(asks_to_change_cart(text))

    def test_a_plain_yes_counts_only_after_the_assistant_offered_the_cart(self):
        offer = "The Downpour Packable Shell comes in navy. Do you want it in your cart?"
        for text in ("Yes please.", "Sure, go ahead.", "Okay", "yes"):
            with self.subTest(text=text):
                self.assertTrue(asks_to_change_cart(text, offer))
                self.assertFalse(asks_to_change_cart(text, "Yes, it comes in navy and black."))
                self.assertFalse(asks_to_change_cart(text, ""))
        # The offer must be a question about the cart, not a cart sentence.
        self.assertFalse(
            asks_to_change_cart("Yes", "Added the Downpour Packable Shell to your cart.")
        )
        self.assertFalse(asks_to_change_cart("No thanks", offer))

    async def test_adding_more_than_the_stock_says_so_in_mock_too(self):
        """Quantity follow-up: "Add five more." got a color list, and only the unchanged cart
        box showed that nothing happened. The mock now asks for the quantity like the model."""
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        await shopper.say("Is medium available here?")
        added = await shopper.say("Great, add it to the cart.")
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in added["cart"]],
            [("a-downpour-packable-navy-m", 1)],
        )
        more = await shopper.say("Add five more.")
        self.assertEqual(more["status"], "complete")
        self.assertIn(
            "You already have 1 in your cart and only 4 are in stock, so I did not add 5.",
            more["answer"],
        )
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in more["cart"]],
            [("a-downpour-packable-navy-m", 1)],
        )
        two = await shopper.say("Add two more.")
        self.assertIn(
            "Added 2 of the Downpour Packable Shell in navy M, $89.00 each, to your cart. You have 3 items totaling $267.00.",
            two["answer"],
        )

    async def test_an_unrequested_add_is_refused_and_the_cart_stays(self):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        await shopper.say("Is medium available here?")
        added = await shopper.say("Great, add it to the cart.")
        self.assertEqual(len(added["cart"]), 1)
        seen = []

        class Eager(MockBackend):
            """Searches on a budget change, then adds the cheapest jacket unasked."""

            def __init__(self):
                self.script = [
                    Decision(
                        tool="search_products",
                        arguments={"query": "rain jacket", "max_price_minor": 10000},
                    ),
                    Decision(
                        tool="add_to_cart",
                        arguments={
                            "product_id": "a-drizzle-anorak",
                            "variant_id": "a-drizzle-anorak-green-m",
                        },
                    ),
                    Decision(answer="Fixture answer: Under 100 dollars, the Drizzle Anorak."),
                ]

            async def decide(self, messages, tools, tool_choice="auto"):
                if messages[-1]["role"] == "tool":
                    seen.append(json.loads(messages[-1]["content"]))
                return self.script.pop(0)

        shopper.backend = Eager()
        result = await shopper.say("Actually, I want to stay under 100 dollars.")
        self.assertEqual(result["status"], "complete")
        denied = [e for e in result["events"] if e["event_type"] == "tool.denied"]
        self.assertEqual(
            [(e["tool_name"], e["reason"]) for e in denied], [("add_to_cart", "cart_not_requested")]
        )
        self.assertEqual(seen[-1]["error"], "not_requested")
        self.assertEqual(
            [(i["variant_id"], i["quantity"]) for i in result["cart"]],
            [("a-downpour-packable-navy-m", 1)],
        )
        self.assertEqual(shopper.conversation.cart, added["cart"])
        self.assertEqual(result["answer"], "Fixture answer: I have not changed your cart.")

    async def test_refused_adds_never_corner_the_model_into_a_failed_turn(self):
        """After a refused cart change, require text and preserve room for a format retry."""
        for first, markup in ((True, False), (True, True), (False, False), (False, True)):
            with self.subTest(search_first=first, markup=markup):
                shopper = Shopper()
                await shopper.say(RAIN)
                offered = []
                add = Decision(
                    tool="add_to_cart",
                    arguments={
                        "product_id": "a-drizzle-anorak",
                        "variant_id": "a-drizzle-anorak-green-m",
                    },
                )
                search = Decision(
                    tool="search_products",
                    arguments={"query": "rain jacket", "max_price_minor": 10000},
                )

                class Insistent(MockBackend):
                    def __init__(self):
                        self.script = ([search] if first else []) + [add]
                        self.marked = markup

                    async def decide(self, messages, tools, tool_choice="auto"):
                        offered.append(({t["function"]["name"] for t in tools}, tool_choice))
                        if self.script:
                            return self.script.pop(0)
                        if tool_choice == "none" and self.marked:
                            self.marked = False
                            return Decision(malformed="unexpected_model_markup")
                        if tool_choice == "none":
                            return Decision(answer="Fixture answer: Under 100, the Drizzle Anorak.")
                        return add  # it would keep trying while tools are allowed

                shopper.backend = Insistent()
                result = await shopper.say("Let's keep it under 100 dollars.")
                self.assertEqual(result["status"], "complete", result.get("reason"))
                refused_at = 1 if first else 0
                self.assertEqual(offered[refused_at][1], "auto")
                self.assertTrue(all(choice == "none" for _, choice in offered[refused_at + 1 :]))
                self.assertEqual(result["cart"], [])
                self.assertEqual(
                    [e["reason"] for e in result["events"] if e["event_type"] == "tool.denied"],
                    ["cart_not_requested"],
                )
