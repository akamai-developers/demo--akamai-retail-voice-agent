import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from fastapi.testclient import TestClient
from PIL import Image

from retail_voice.api import create_app
from retail_voice.catalog_repository import default_repository
from retail_voice.models.mock import MockBackend
from retail_voice.tryon import TryOn, requested
from retail_voice.tryon_service import create_app as create_image_app

KEYS = {"tenant-a-test-key-0001": "retailer-a", "tenant-b-test-key-0002": "retailer-b"}


class TryOnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        image = io.BytesIO()
        Image.new("RGB", (32, 48), "navy").save(image, "JPEG")
        (self.root / "preview.jpg").write_bytes(image.getvalue())
        (self.root / "previews.json").write_text(
            json.dumps(
                [
                    {"product_id": product, "color": "navy", "file": "preview.jpg"}
                    for product in ("a-downpour-packable", "a-drizzle-anorak")
                ]
            )
        )
        self.backend = MockBackend()
        self.provider = TryOn(
            default_repository(), self.backend, mode="prepared", directory=self.root
        )
        self.env = patch.dict(os.environ, {"DEMO_MODE": "mock"})
        self.env.start()
        self.client = TestClient(
            create_app(KEYS, "operator-test-key-0003", backend=self.backend, tryon=self.provider)
        )
        self.client.__enter__()
        self.session = self.start("a")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def start(self, tenant):
        return self.client.post(
            "/v1/sessions",
            json={},
            headers={
                "Authorization": f"Bearer tenant-{tenant}-test-key-000{1 if tenant == 'a' else 2}",
                "Idempotency-Key": "setup",
            },
        ).json()

    def send(self, text, session=None, key="turn", inline=False):
        session = session or self.session
        response = self.client.post(
            f"/v1/sessions/{session['session_id']}/messages",
            headers={
                "Authorization": "Bearer " + session["session_token"],
                "Idempotency-Key": key,
                "X-Inline-Reply": "1" if inline else "0",
            },
            files={"text": (None, text), "selected_product_id": (None, "a-downpour-packable")},
        )
        self.assertEqual(response.status_code, 200)
        return [json.loads(line) for line in response.text.splitlines()]

    def test_voice_wording_resolves_preview_and_keeps_media_out_of_trace(self):
        events = self.send("I like this item, let me see it on me in navy medium.")
        headers = {"Authorization": "Bearer " + self.session["session_token"]}
        result = self.client.get(events[-1]["result_path"], headers=headers).json()
        schema = json.loads(Path("contracts/message-result.schema.json").read_text())
        jsonschema.validate(result, schema)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["preview"]["kind"], "prepared")
        self.assertIn("prepared preview", result["answer"])
        self.assertNotIn("image", result)
        self.assertNotIn("preview_image", result)
        self.assertNotIn(str(self.root), json.dumps(events))
        event_schema = json.loads(Path("contracts/event.schema.json").read_text())
        for event in events:
            jsonschema.validate(event, event_schema)
        path = result["preview"]["image_path"]
        self.assertEqual(self.client.get(path).status_code, 401)
        other = self.start("b")
        self.assertEqual(
            self.client.get(
                path, headers={"Authorization": "Bearer " + other["session_token"]}
            ).status_code,
            403,
        )
        response = self.client.get(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.size, (32, 48))
        self.assertEqual(self.send("I like this item, let me see it on me in navy medium."), events)
        self.client.delete(f"/v1/sessions/{self.session['session_id']}", headers=headers)
        self.assertEqual(self.client.get(path, headers=headers).status_code, 403)

    def test_inline_result_is_replayable_without_private_commit_or_preview_bytes(self):
        frames = self.send("Show it on me in navy medium.", inline=True)
        reply = frames[-1]
        self.assertEqual(reply["type"], "reply")
        self.assertTrue(reply["audio_base64"])
        self.assertNotIn("commit", reply["result"])
        self.assertNotIn("preview_image", reply["result"])
        headers = {"Authorization": "Bearer " + self.session["session_token"]}
        self.assertEqual(
            reply["result"], self.client.get(frames[-2]["result_path"], headers=headers).json()
        )
        self.assertEqual(frames, self.send("Show it on me in navy medium.", inline=True))

    def test_unsupported_color_does_not_substitute_a_garment(self):
        events = self.send("Show it on me in purple.")
        result = self.client.get(
            events[-1]["result_path"],
            headers={"Authorization": "Bearer " + self.session["session_token"]},
        ).json()
        self.assertTrue(result["clarification"])
        self.assertNotIn("preview", result)

    def test_reported_conversation_keeps_product_color_size_and_cart(self):
        script = [
            "Hi, I'm looking for a Yavy raincoat that's under the hundred and fifty dollars.",
            "How about the downpour packable? Do you have that in Navy",
            "I'm looking for the downpour packable shell raincoat. Do you have that in navy?",
            "How about medium?",
            "Let's check the Navy medium and let me try it on.",
            "It looks great. Let's add it to my cart.",
        ]
        results = []
        for number, text in enumerate(script):
            response = self.client.post(
                f"/v1/sessions/{self.session['session_id']}/messages",
                headers={
                    "Authorization": "Bearer " + self.session["session_token"],
                    "Idempotency-Key": f"flow-{number}",
                    "X-Inline-Reply": "1",
                },
                files={"text": (None, text)},
            )
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.text.splitlines()[-1])["result"]
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["spoken_answer"], result["answer"])
            results.append(result)
        for result in results[1:]:
            self.assertEqual(result["selected_product_id"], "a-downpour-packable")
        for result in results[1:3]:
            self.assertIn("comes in navy", result["answer"])
            self.assertIsNone(result["selected_variant_id"])
            self.assertFalse(result["clarification"])
        for result in results[3:]:
            self.assertEqual(result["selected_variant_id"], "a-downpour-packable-navy-m")
        self.assertEqual(results[3]["stock_checks"][0]["quantity"], 4)
        self.assertEqual(results[4]["stock_checks"][0]["quantity"], 4)
        self.assertEqual(results[4]["preview"]["color"], "navy")
        self.assertEqual(results[-1]["cart"][0]["variant_id"], "a-downpour-packable-navy-m")
        self.assertEqual(results[-1]["cart"][0]["quantity"], 1)

    def test_preview_does_not_choose_a_size_for_the_shopper(self):
        frames = self.send("Show the downpour packable on me in navy.", inline=True)
        self.assertIsNone(frames[-1]["result"]["selected_variant_id"])
        frames = self.send("Add it to the cart.", key="add", inline=True)
        self.assertEqual(frames[-1]["result"]["cart"], [])
        self.assertTrue(frames[-1]["result"]["clarification"])

    def flow_turn(self, text, index):
        response = self.client.post(
            f"/v1/sessions/{self.session['session_id']}/messages",
            headers={
                "Authorization": "Bearer " + self.session["session_token"],
                "Idempotency-Key": f"new-flow-{index}",
                "X-Inline-Reply": "1",
            },
            files={"text": (None, text)},
        )
        self.assertEqual(response.status_code, 200)
        frames = [json.loads(line) for line in response.text.splitlines()]
        result = frames[-1]["result"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["answer"], result["spoken_answer"])
        return result, frames[:-1]

    def test_stock_first_preview_product_switch_and_cart_reported_nine_turns(self):
        script = [
            "Hi, how are you?",
            "I'm looking for a raincoat.",
            "Let's go with the downpour packable shell in medium and I want to try it on.",
            "Okay, this actually looks good. Let's add it to my cart.",
            "Well, let's go with the medium yellow. I want to try it on.",
            "Perfect. Let's add it to my cart.",
            "Oh wow, I thought she said it was in stock.",
            "Okay, let's try the drizzle an Iraq and Navy and I want to try it on.",
            "Okay, that looks good. Let's add it to my cart.",
        ]
        results = []
        with patch.object(self.provider, "create", wraps=self.provider.create) as generate:
            for index, text in enumerate(script):
                if index < 2:
                    result, _ = self.flow_turn(text, index)
                else:
                    with patch.object(
                        self.backend,
                        "decide",
                        side_effect=AssertionError("action must not depend on model history"),
                    ):
                        result, _ = self.flow_turn(text, index)
                results.append(result)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(generate.call_args.args, ("retailer-a", "a-drizzle-anorak", "navy"))
        self.assertNotIn("*", results[1]["answer"])
        self.assertLess(len(results[1]["answer"].split()), 30)
        self.assertIn("navy is in stock", results[2]["answer"])
        self.assertIn("yellow is sold out", results[2]["answer"])
        for result in results[2:7]:
            self.assertNotIn("preview", result)
            self.assertEqual(result["cart"], [])
        for result in results[4:7]:
            self.assertIn("out of stock", result["answer"])
        self.assertIn("Sorry", results[6]["answer"])
        self.assertEqual(results[7]["preview"]["product_id"], "a-drizzle-anorak")
        self.assertEqual(results[7]["selected_variant_id"], "a-drizzle-anorak-navy-m")
        self.assertNotIn("preview", results[8])
        self.assertEqual(results[8]["cart"][0]["variant_id"], "a-drizzle-anorak-navy-m")
        self.assertEqual(results[8]["cart"][0]["quantity"], 1)

    def test_accept_stocked_alternative_then_add_and_reuse_preview_with_fresh_stock(self):
        result, _ = self.flow_turn("Try the Downpour Packable Shell in medium on me.", 0)
        self.assertNotIn("preview", result)
        self.assertIsNone(result["selected_variant_id"])
        with patch.object(self.provider, "create", wraps=self.provider.create) as generate:
            result, _ = self.flow_turn("Yes please.", 1)
            self.assertEqual(result["preview"]["color"], "navy")
            result, _ = self.flow_turn("Add it to my cart.", 2)
            self.assertEqual(result["cart"][0]["variant_id"], "a-downpour-packable-navy-m")
            result, events = self.flow_turn("Show it on me again.", 3)
            self.assertEqual(generate.call_count, 1)
            self.assertTrue(
                next(e for e in events if e["event_type"] == "preview.completed")["cache_hit"]
            )
            fact = self.provider.catalog.availability(
                "retailer-a", "a-downpour-packable", "a-downpour-packable-navy-m", "store-a"
            )
            fact = dict(fact, quantity=0, in_stock=False)
            with patch.object(type(self.provider.catalog), "availability", return_value=fact):
                result, _ = self.flow_turn("Show it on me again.", 4)
            self.assertNotIn("preview", result)
            self.assertEqual(generate.call_count, 1)

    def test_new_unrecognized_item_cannot_reuse_old_preview(self):
        self.flow_turn("Show the Downpour Packable Shell in navy medium on me.", 0)
        result, _ = self.flow_turn("Try the Thunder coat and show it on me.", 1)
        self.assertNotIn("preview", result)
        self.assertIsNone(result["selected_product_id"])
        result, _ = self.flow_turn("Add it.", 2)
        self.assertEqual(result["cart"], [])

    def test_cancelled_preview_clarification_does_not_resume_on_later_size(self):
        self.flow_turn("Try the Downpour Packable Shell in navy on me.", 0)
        self.flow_turn("Don't try it on. Just tell me about it.", 1)
        result, _ = self.flow_turn("Medium please.", 2)
        self.assertNotIn("preview", result)

    def test_missing_size_cart_can_be_completed_by_a_short_answer(self):
        self.flow_turn("Add the Downpour Packable Shell in navy to my cart.", 0)
        result, _ = self.flow_turn("Medium please.", 1)
        self.assertEqual(result["cart"][0]["variant_id"], "a-downpour-packable-navy-m")

    def test_exact_variant_uses_its_color_instead_of_the_first_product_photo(self):
        frames = self.send("Show a-downpour-packable-navy-m on me.", inline=True)
        self.assertEqual(frames[-1]["result"]["preview"]["color"], "navy")

    def test_missing_prepared_color_never_substitutes_the_available_preview(self):
        frames = self.send("Show a-downpour-packable-yellow-s on me.", inline=True)
        self.assertNotIn("preview", frames[-1]["result"])
        self.assertIn("not available", frames[-1]["result"]["answer"])

    def test_manifest_cannot_escape_private_directory(self):
        (self.root / "previews.json").write_text(
            json.dumps(
                [{"product_id": "a-downpour-packable", "color": "navy", "file": "../outside.jpg"}]
            )
        )
        with self.assertRaises((ValueError, FileNotFoundError)):
            TryOn(default_repository(), self.backend, mode="prepared", directory=self.root)

    def test_request_detection(self):
        for text in ("Let me see it on me", "Can I try it on?", "Show me wearing this jacket"):
            self.assertTrue(requested(text), text)
        self.assertFalse(requested("Don't show it on me"))
        self.assertFalse(requested("Show me navy jackets"))

    def test_private_image_worker_validates_inputs_before_inference(self):
        source = (self.root / "preview.jpg").read_bytes()

        class Runtime:
            lock = threading.Lock()
            pipeline = None
            calls = 0

            def load(self):
                self.pipeline = object()

            def run(self, person, garment, name, color):
                self.calls += 1
                self.asserted = (name, color)
                return source

        runtime = Runtime()
        with TestClient(create_image_app(runtime)) as client:
            self.assertTrue(client.get("/health").json()["ready"])
            files = {"person": ("person.jpg", source), "garment": ("garment.jpg", source)}
            response = client.post(
                "/try-on", files=files, data={"garment_name": "Rain shell", "color": "navy"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, source)
            self.assertEqual(runtime.calls, 1)
            bad = files | {"person": ("person.jpg", b"invalid")}
            self.assertEqual(
                client.post(
                    "/try-on", files=bad, data={"garment_name": "Rain shell", "color": "navy"}
                ).status_code,
                400,
            )
            self.assertEqual(runtime.calls, 1)
