"""Shopper intent wins over model guesses and stale conversation preferences."""

import unittest

from test_conversation import RAIN, Shopper

from retail_voice.catalog import explicit_search_budget
from retail_voice.models.base import Decision
from retail_voice.models.mock import MockBackend


class ScriptedAdd(MockBackend):
    def __init__(self, arguments):
        self.script = [Decision(tool="add_to_cart", arguments=arguments), Decision(answer="Done.")]

    async def decide(self, *args, **kwargs):
        return self.script.pop(0)


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_changed_budget_refreshes_cards_when_model_skips_search(self):
        class SkipsSearch(MockBackend):
            async def decide(self, *args, **kwargs):
                return Decision(answer="The previous jacket should work. Anything else?")

        shopper = Shopper(tenant="retailer-b")
        await shopper.say(RAIN)
        shopper.backend = SkipsSearch()
        result = await shopper.say("Actually, keep it under 125 dollars.")
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["product_cards"])
        self.assertTrue(all(p["price_minor"] <= 12500 for p in result["product_cards"]))
        self.assertTrue(any(e.get("tool_call_id") == "gateway-search" for e in result["events"]))
        result = await shopper.say("Do you have any rain jackets under 50 dollars?")
        self.assertEqual(result["product_cards"], [])
        self.assertIn("no matching items", result["answer"])

    async def test_search_cannot_use_an_old_budget(self):
        class WrongBudget(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                decision = await super().decide(messages, tools, tool_choice)
                if decision.tool == "search_products":
                    decision.arguments["max_price_minor"] = 15000
                return decision

        result = await Shopper(backend=WrongBudget(), tenant="retailer-b").say(
            "Do you have any rain jackets under 50 dollars?"
        )
        self.assertEqual(result["product_cards"], [])

    def test_budget_detection_preserves_item_questions_and_ambiguity(self):
        self.assertEqual(explicit_search_budget("Show jackets under $99.95"), 9995)
        for text in (
            "Is this jacket under $100?",
            "Do not show jackets under $50",
            "Show jackets under $50 or under $100",
            "Show jackets under $0",
            "Show jackets under $1,000",
        ):
            self.assertIsNone(explicit_search_budget(text))

    async def test_search_speaks_the_complete_displayed_reply(self):
        class LongAnswer(MockBackend):
            speech = None

            async def decide(self, messages, tools, tool_choice="auto"):
                decision = await super().decide(messages, tools, tool_choice)
                if decision.answer:
                    decision.answer = (
                        "Here are the options. "
                        + "The catalog has lightweight jackets for rainy days. " * 8
                    )
                return decision

            async def synthesize(self, text):
                self.speech = text
                return await super().synthesize(text)

        backend = LongAnswer()
        result = await Shopper(backend=backend).say(RAIN)
        self.assertEqual(result["answer"], result["spoken_answer"])
        self.assertEqual(backend.speech, result["answer"])
        self.assertEqual(len(result["product_cards"]), 3)

    async def selected(self, settled=True):
        shopper = Shopper()
        await shopper.say(RAIN)
        await shopper.say("Does the second one come in navy?")
        if settled:
            await shopper.say("Is medium available here?")
        return shopper

    async def test_negative_and_deferred_cart_requests_never_add(self):
        for text in (
            "Don't add it to the cart.",
            "Do not buy it.",
            "Add it to the cart later.",
            "Show me my cart.",
            "Remove it from my cart.",
            "Don't add the Downpour Packable Shell in yellow large.",
        ):
            with self.subTest(text=text):
                shopper = await self.selected()
                shopper.backend = ScriptedAdd(
                    {
                        "product_id": "a-downpour-packable",
                        "variant_id": "a-downpour-packable-navy-m",
                    }
                )
                result = await shopper.say(text)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["cart"], [])
                self.assertIn("not changed", result["answer"])
                self.assertEqual(result["selected_variant_id"], "a-downpour-packable-navy-m")

    async def test_missing_size_cannot_be_guessed(self):
        shopper = await self.selected(settled=False)
        result = await shopper.say("Add it to the cart.")
        self.assertEqual(result["cart"], [])
        self.assertTrue(result["clarification"])

    async def test_negative_request_and_model_claim_cannot_repeat_a_past_addition(self):
        class RepeatsOldAnswer(MockBackend):
            async def decide(self, *args, **kwargs):
                return Decision(answer="Added the Downpour Packable Shell to your cart.")

        shopper = await self.selected()
        await shopper.say("Add it to my cart.")
        shopper.backend = RepeatsOldAnswer()
        for text in ("Don't add another one.", "Okay, thanks."):
            result = await shopper.say(text)
            self.assertEqual(result["cart"][0]["quantity"], 1)
            self.assertIn("not changed your cart", result["answer"])
            self.assertNotIn("Added", result["answer"])

    async def test_cart_uses_resolved_choice_without_consulting_a_model_that_would_substitute(self):
        for arguments in (
            {"product_id": "a-drizzle-anorak", "variant_id": "a-drizzle-anorak-green-m"},
            {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-yellow-m"},
            {
                "product_id": "a-downpour-packable",
                "variant_id": "a-downpour-packable-navy-m",
                "quantity": 2,
            },
        ):
            with self.subTest(arguments=arguments):
                shopper = await self.selected()
                shopper.backend = ScriptedAdd(arguments)
                result = await shopper.say("Add one of the second one to my cart.")
                self.assertEqual(len(result["cart"]), 1)
                self.assertEqual(result["cart"][0]["variant_id"], "a-downpour-packable-navy-m")
                self.assertEqual(result["cart"][0]["quantity"], 1)
                self.assertEqual(len(shopper.backend.script), 2)

    async def test_explicit_variant_overrides_previous_color_and_size(self):
        shopper = Shopper(tenant="retailer-b")
        await shopper.say("Is jacket-b available in navy medium at this store?")
        result = await shopper.say("Is jacket-b-red-m in stock here?")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stock_checks"][0]["variant_id"], "jacket-b-red-m")
        self.assertEqual(result["stock_checks"][0]["quantity"], 0)
        self.assertIn("out of stock", result["answer"])

    async def test_current_contradiction_still_requires_clarification(self):
        shopper = Shopper(tenant="retailer-b")
        await shopper.say("Is jacket-b available in navy medium at this store?")
        result = await shopper.say("Is jacket-b-red-m in stock in navy?")
        self.assertEqual(result["stock_checks"], [])
        self.assertTrue(result["clarification"])
