import base64
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from retail_voice.api import MAX_BODY, create_app, fixture_media
from retail_voice.auth import Credentials
from retail_voice.models.base import Decision
from retail_voice.models.mock import MockBackend

KEYS = {"tenant-a-test-key-0001": "retailer-a", "tenant-b-test-key-0002": "retailer-b"}
OPERATOR = "operator-test-key-0003"
ROOT = Path(__file__).resolve().parents[1]


class CountingBackend(MockBackend):
    calls = 0

    async def transcribe(self, audio):
        self.calls += 1
        return await super().transcribe(audio)


class APITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DEMO_MODE": "mock"})
        self.env.start()
        self.backend = CountingBackend()
        self.app = create_app(KEYS, OPERATOR, backend=self.backend)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()

    def start(self, key="tenant-a-test-key-0001", idem="setup"):
        return self.client.post(
            "/v1/sessions",
            headers={"Authorization": "Bearer " + key, "Idempotency-Key": idem},
            json={},
        )

    def fixture(self, s, key="turn", foreign=False):
        return self.client.post(
            f"/v1/sessions/{s['session_id']}/fixture-turn",
            headers={"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": key},
            json={
                "sku": "jacket-b" if foreign else "jacket-a",
                "store_id": "store-b" if foreign else "store-a",
            },
        )

    def test_startup_rejects_empty_short_duplicate_credentials(self):
        for operator in ["", "short", "tenant-a-test-key-0001", "é" * 20]:
            with self.assertRaises(RuntimeError):
                create_app(KEYS, operator)
        with self.assertRaises(RuntimeError):
            create_app({}, OPERATOR)

    def test_explicit_mode_required(self):
        with patch.dict(os.environ, {"DEMO_MODE": "nvidia"}):
            with self.assertRaises(RuntimeError):
                create_app(KEYS, OPERATOR)

    def test_bad_auth_and_non_ascii_are_401(self):
        for header in [None, "Bearer ", "Bearer " + "é" * 20, "Bearer wrong-test-key-123"]:
            try:
                Credentials(KEYS, OPERATOR).tenant(header)
            except Exception as exc:
                self.assertEqual(exc.status_code, 401)
            else:
                self.fail("credential accepted")
        r = self.client.post(
            "/v1/sessions", headers=[(b"authorization", b"Bearer " + b"\xff" * 20)], json={}
        )
        self.assertEqual(r.status_code, 401)

    def test_body_cannot_override_tenant(self):
        r = self.client.post(
            "/v1/sessions",
            headers={"Authorization": "Bearer tenant-a-test-key-0001", "Idempotency-Key": "setup"},
            json={"tenant": "retailer-b"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.backend.calls, 0)

    def test_setup_contract_and_retry(self):
        a, b = self.start().json(), self.start().json()
        jsonschema.validate(
            a, json.loads((ROOT / "contracts/local-session.schema.json").read_text())
        )
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertEqual(a["max_turns"], 16)
        # both tenants retain a session; a second session for one tenant is refused.
        other = self.start("tenant-b-test-key-0002")
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["retained_sessions"], {"used": 2, "max": 2})
        again = self.start(idem="second")
        self.assertEqual(
            (again.status_code, again.json()["detail"]), (409, "tenant_session_active")
        )

    def test_operator_readiness_reports_health_and_occupancy_separately(self):
        h = {"Authorization": "Bearer " + OPERATOR}
        empty = self.client.get("/admin/ready", headers=h).json()
        self.assertEqual(empty["backend_ready"], True)
        self.assertEqual(empty["retained_sessions"]["used"], 0)
        self.assertEqual(empty["retained_sessions"]["max"], 2)
        self.assertEqual((empty["executing"], empty["queued"], empty["drain"]), (0, 0, False))
        self.start()
        self.start("tenant-b-test-key-0002")
        full = self.client.get("/admin/ready", headers=h).json()
        self.assertEqual(full["retained_sessions"]["used"], 2)
        self.assertEqual(
            full["retained_sessions"]["per_tenant"],
            {"retailer-a": {"used": 1, "max": 1}, "retailer-b": {"used": 1, "max": 1}},
        )
        self.assertTrue(full["backend_ready"])

    def test_operator_auth_and_strict_body(self):
        for token, expected in [("", 401), ("tenant-a-test-key-0001", 403), (OPERATOR, 200)]:
            r = self.client.put(
                "/admin/admission",
                headers={"Authorization": "Bearer " + token},
                json={"drain": True},
            )
            self.assertEqual(r.status_code, expected)
        self.assertEqual(self.start().status_code, 409)
        r = self.client.put(
            "/admin/admission",
            headers={"Authorization": "Bearer " + OPERATOR},
            json={"drain": "false"},
        )
        self.assertEqual(r.status_code, 400)

    def test_complete_pipeline_retry_schema_and_protected_audio(self):
        s = self.start().json()
        first = self.fixture(s)
        self.assertEqual(first.status_code, 200)
        events = [json.loads(line) for line in first.text.splitlines()]
        schema = json.loads((ROOT / "contracts/event.schema.json").read_text())
        for event in events:
            jsonschema.validate(event, schema)
        self.assertEqual(events[-1]["event_type"], "turn.completed")
        self.assertEqual([e["sequence"] for e in events], list(range(len(events))))
        self.assertEqual(events[0]["span_id"], events[-1]["parent_span_id"])
        self.assertEqual(self.fixture(s).text, first.text)
        self.assertEqual(self.backend.calls, 1)
        self.assertNotIn(s["session_token"], first.text)
        self.assertNotIn("transcript", first.text)
        url = events[-1]["result_path"]
        h = {"Authorization": "Bearer " + s["session_token"]}
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(
            self.client.get(
                url, headers={"Authorization": "Bearer tenant-b-test-key-0002"}
            ).status_code,
            403,
        )
        result = self.client.get(url, headers=h).json()
        self.assertEqual(result["tool_result"]["navy_medium"], 3)
        self.assertEqual(result["audio_kind"], "test_tone")
        self.assertEqual(self.client.get(result["audio_path"], headers=h).content[:4], b"RIFF")
        self.assertEqual(self.client.get(result["audio_path"]).status_code, 401)
        self.client.delete(f"/v1/sessions/{s['session_id']}", headers=h)
        self.assertEqual(self.client.get(url, headers=h).status_code, 403)

    def test_foreign_catalog_fails_without_success_or_audio(self):
        s = self.start().json()
        r = self.fixture(s, foreign=True)
        events = [json.loads(line) for line in r.text.splitlines()]
        self.assertIn("tool.denied", [e["event_type"] for e in events])
        self.assertEqual(events[-1]["event_type"], "turn.failed")
        self.assertNotIn("tts.completed", r.text)
        self.assertNotIn("turn.completed", r.text)

    def test_reused_turn_key_with_changed_payload_conflicts(self):
        s = self.start().json()
        self.fixture(s)
        self.assertEqual(self.fixture(s, foreign=True).status_code, 409)
        self.assertEqual(self.backend.calls, 1)

    def test_multipart_media_and_metadata_stripping(self):
        s = self.start().json()
        audio, _ = fixture_media()
        output = io.BytesIO()
        info = PngImagePlugin.PngInfo()
        info.add_text("private", "EXAMPLE_LOCATION")
        Image.new("RGB", (10, 10)).save(output, format="PNG", pnginfo=info)
        observed = []
        original = self.backend.decide

        async def inspect(messages, *args):
            # The photo reaches the model inside the legacy prompt as a data URI.
            url = messages[1]["content"][1]["image_url"]["url"]
            observed.append(base64.b64decode(url.split(",", 1)[1]))
            return await original(messages, *args)

        self.backend.decide = inspect
        r = self.client.post(
            f"/v1/sessions/{s['session_id']}/turns",
            headers={"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": "media"},
            files={"audio": ("audio.wav", audio), "image": ("photo.png", output.getvalue())},
            data={"sku": "jacket-a", "store_id": "store-a"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("turn.completed", r.text)
        self.assertNotIn(b"EXAMPLE_LOCATION", observed[0])
        self.assertEqual(Image.open(io.BytesIO(observed[0])).info, {})

    def test_large_photo_is_resized_before_model_input(self):
        from retail_voice.media import sanitize_image

        raw = io.BytesIO()
        Image.new("RGB", (2000, 1500), "navy").save(raw, format="PNG")
        clean = Image.open(io.BytesIO(sanitize_image(raw.getvalue())))
        self.assertEqual(clean.size, (1024, 768))

    def test_malformed_and_oversized_media_rejected_before_inference(self):
        s = self.start().json()
        h = {"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": "media"}
        url = f"/v1/sessions/{s['session_id']}/turns"
        r = self.client.post(
            url,
            headers=h,
            files={"audio": ("a.wav", b"bad"), "image": ("a.png", b"bad")},
            data={"sku": "jacket-a", "store_id": "store-a"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            self.client.post(url, headers=h, content=b"x" * (MAX_BODY + 1)).status_code, 413
        )
        self.assertEqual(self.backend.calls, 0)

    def test_assets_and_security_headers(self):
        for path in [
            "/",
            "/app/app.js",
            "/app/transport.js",
            "/app/capture.js",
            "/app/conversation.js",
            "/app/diagnostics.js",
            "/app/architecture-flow.js",
            "/app/architecture-flow.css",
            "/app/app.css",
            "/app/brand/akamai-logo-white.svg",
            "/app/fonts/InstrumentSans.ttf",
            "/app/fonts/JetBrainsMono.ttf",
        ]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])


class MessageAPITests(unittest.TestCase):
    """POST /v1/sessions/{id}/messages and the extended result, in mock mode."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"DEMO_MODE": "mock"})
        self.env.start()
        self.decisions = 0
        outer = self

        class Counting(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                outer.decisions += 1
                return await super().decide(messages, tools, tool_choice)

        self.backend = Counting()
        self.app = create_app(KEYS, OPERATOR, backend=self.backend)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.schema = json.loads((ROOT / "contracts/message-result.schema.json").read_text())
        self.event_schema = json.loads((ROOT / "contracts/event.schema.json").read_text())

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()

    def start(self, key="tenant-a-test-key-0001"):
        r = self.client.post(
            "/v1/sessions",
            headers={"Authorization": "Bearer " + key, "Idempotency-Key": "setup-" + key[-4:]},
            json={},
        )
        self.assertEqual(r.status_code, 200)
        return r.json()

    def send(self, s, idem, text=None, audio=None, fields=None, **named):
        parts = {}
        if text is not None:
            parts["text"] = (None, text)
        if audio is not None:
            parts["audio"] = ("question.wav", audio, "audio/wav")
        for name, value in (fields or named).items():
            parts[name] = value if isinstance(value, tuple) else (None, str(value))
        return self.client.post(
            f"/v1/sessions/{s['session_id']}/messages",
            headers={"Authorization": "Bearer " + s["session_token"], "Idempotency-Key": idem},
            files=parts,
        )

    def result(self, s, response):
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        for event in events:
            jsonschema.validate(event, self.event_schema)
        h = {"Authorization": "Bearer " + s["session_token"]}
        body = self.client.get(events[-1]["result_path"], headers=h).json()
        jsonschema.validate(body, self.schema)
        return events, body

    def test_fixture_conversation_over_the_messages_route(self):
        fixture = json.loads((ROOT / "contracts/fixture.json").read_text())["conversation"]
        s = self.start()
        revision = 0
        for index, step in enumerate(fixture["messages"]):
            events, body = self.result(
                s, self.send(s, f"m{index}", step["text"], display_revision=revision)
            )
            expect = step["expect"]
            tools = [e["tool_name"] for e in events if e["event_type"] == "tool.completed"]
            self.assertEqual(tools, expect["tools"])
            self.assertEqual(body["status"], "complete")
            self.assertEqual((body["input_source"], body["transcript"]), ("text", step["text"]))
            self.assertEqual((body["mode"], body["site"]), ("mock", "local-mock"))
            self.assertEqual(body["audio_kind"], "test_tone")
            if "product_ids" in expect:
                self.assertEqual(
                    [c["product_id"] for c in body["product_cards"]], expect["product_ids"]
                )
            if "display_revision" in expect:
                self.assertEqual(body["display_revision"], expect["display_revision"])
            if "selected_product_id" in expect:
                self.assertEqual(body["selected_product_id"], expect["selected_product_id"])
            if "selected_variant_id" in expect:
                self.assertEqual(body["selected_variant_id"], expect["selected_variant_id"])
            if "stock_checks" in expect:
                self.assertEqual(body["stock_checks"], expect["stock_checks"])
            if "answer" in expect:
                self.assertEqual(body["answer"], expect["answer"])
            if "stock" in expect:
                check = body["stock_checks"][0]
                self.assertEqual({k: check[k] for k in expect["stock"]}, expect["stock"])
            revision = body["display_revision"]
            self.assertNotIn("commit", body)
            self.assertNotIn(s["session_token"], json.dumps(events))
        a_key, b_key = (
            {"Authorization": "Bearer tenant-a-test-key-0001"},
            {"Authorization": "Bearer tenant-b-test-key-0002"},
        )
        for card in body["product_cards"]:
            for path in [card["image_path"]] + [v["image_path"] for v in card["variants"]]:
                self.assertEqual(self.client.get(path, headers=a_key).status_code, 200)
                self.assertEqual(self.client.get(path, headers=b_key).status_code, 404)

    def test_new_position_wins_over_echoed_selection_for_text_and_audio(self):
        s = self.start()
        _, shown = self.result(
            s, self.send(s, "search", "lightweight rain jacket under 150 dollars")
        )
        revision = shown["display_revision"]
        _, second = self.result(
            s,
            self.send(s, "second", "Does the second one come in navy?", display_revision=revision),
        )
        self.assertEqual(second["selected_product_id"], "a-downpour-packable")
        for index, source in enumerate(("text", "audio")):
            with self.subTest(source=source):
                question = "Actually, tell me about the third one"
                fields = {
                    "selected_product_id": second["selected_product_id"],
                    "display_revision": revision,
                }
                with patch.object(self.backend, "transcribe", return_value=question):
                    response = self.send(
                        s,
                        f"third-{index}",
                        text=question if source == "text" else None,
                        audio=fixture_media()[0] if source == "audio" else None,
                        **fields,
                    )
                    _, body = self.result(s, response)
                self.assertEqual(
                    body["selected_product_id"], shown["product_cards"][2]["product_id"]
                )
                self.assertIn("Ridgeline Rain Shell", body["answer"])
                self.assertEqual(body["input_source"], source)
        events, stale = self.result(
            s,
            self.send(
                s,
                "stale",
                "Tell me about the first one",
                selected_product_id=body["selected_product_id"],
                display_revision=revision - 1,
            ),
        )
        self.assertTrue(stale["clarification"])
        self.assertIn("screen changed", stale["answer"])
        self.assertNotIn("model.completed", [e["event_type"] for e in events])

    def test_both_tenants_keep_separate_conversations(self):
        a, b = self.start(), self.start("tenant-b-test-key-0002")
        _, ra = self.result(a, self.send(a, "a1", "lightweight rain jacket under 150 dollars"))
        _, rb = self.result(b, self.send(b, "b1", "rain jacket under 150 dollars"))
        b_ids = {
            p["product_id"]
            for p in self.client.get(
                "/v1/catalog", headers={"Authorization": "Bearer tenant-b-test-key-0002"}
            ).json()["products"]
        }
        self.assertTrue(rb["product_cards"])
        self.assertTrue({c["product_id"] for c in rb["product_cards"]} <= b_ids)
        self.assertFalse({c["product_id"] for c in ra["product_cards"]} & b_ids)
        state = self.app.state.admission
        self.assertEqual(state.sessions[a["session_id"]].conversation.display_revision, 1)
        self.assertEqual(state.sessions[b["session_id"]].conversation.display_revision, 1)
        self.assertEqual(len(state.sessions[a["session_id"]].conversation.exchanges), 1)

    def test_store_clock_reaches_the_model_but_never_the_events(self):
        payloads = []
        decide = self.backend.decide

        async def recording(messages, tools, tool_choice="auto"):
            payloads.append(next(m for m in reversed(messages) if m["role"] == "user")["content"])
            return await decide(messages, tools, tool_choice)

        self.backend.decide = recording
        with patch("retail_voice.prompts.now", lambda: 1793657700):
            s = self.start()
            events, body = self.result(s, self.send(s, "clock-1", text="What's the date today?"))
        payload = json.loads(payloads[0])
        self.assertEqual(payload["local_time"], "Monday, November 2, 2026, 3:15 PM")
        self.assertEqual(payload["timezone"], "America/Denver")
        self.assertIn("Monday, November 2, 2026, 3:15 PM", body["answer"])
        self.assertEqual(
            [e["event_type"] for e in events if e["event_type"].startswith("tool")], []
        )
        # The result schema is unchanged (validated above); the events carry no clock text.
        for text in ("local_time", "timezone", "November", "3:15", "America/Denver"):
            self.assertNotIn(text, json.dumps(events))
        self.assertNotIn("local_time", body)

    def test_audio_message_runs_asr(self):
        s = self.start()
        audio, _ = fixture_media()
        events, body = self.result(s, self.send(s, "voice", audio=audio))
        self.assertIn("asr.completed", [e["event_type"] for e in events])
        self.assertEqual(body["input_source"], "audio")
        self.assertTrue(body["transcript"].startswith("Fixture transcript"))
        self.assertEqual(events[0]["input_source"], "audio")

    def test_exactly_one_of_audio_or_text_and_no_other_fields(self):
        s = self.start()
        audio, image = fixture_media()
        cases = [
            ({"text": "rain jacket", "audio": audio}, "audio_or_text_required"),
            ({}, "invalid_multipart"),
            ({"selected_product_id": "jacket-a"}, "audio_or_text_required"),
            ({"text": "rain jacket", "tenant": "retailer-b"}, "unexpected_fields"),
            ({"text": "rain jacket", "sku": "jacket-b"}, "unexpected_fields"),
            ({"text": "rain jacket", "image": ("p.png", image, "image/png")}, "unexpected_fields"),
            ({"text": "rain jacket", "url": "http://example.invalid/x.png"}, "unexpected_fields"),
            ({"text": "x" * 2001}, "text_required_up_to_2000_characters"),
            ({"text": "   "}, "text_required_up_to_2000_characters"),
            (
                {"text": ("t.txt", b"rain jacket", "text/plain")},
                "text_required_up_to_2000_characters",
            ),
            ({"text": "rain jacket", "display_revision": "-1"}, "invalid_display_revision"),
            ({"text": "rain jacket", "display_revision": "abc"}, "invalid_display_revision"),
            ({"text": "rain jacket", "display_revision": "\u00b2"}, "invalid_display_revision"),
        ]
        for index, (fields, detail) in enumerate(cases):
            text = fields.pop("text") if isinstance(fields.get("text"), str) else None
            sent_audio = fields.pop("audio", None)
            r = self.send(s, f"bad{index}", text, sent_audio, fields=fields)
            self.assertEqual((r.status_code, r.json()["detail"]), (400, detail), fields)
        self.assertEqual(self.decisions, 0)
        self.assertEqual(self.app.state.admission.sessions[s["session_id"]].turn_count, 0)

    def test_selected_product_must_belong_to_the_tenant(self):
        s = self.start()
        bodies = []
        for index, product_id in enumerate(["jacket-b", "no-such-product", "../catalog.json"]):
            r = self.send(s, f"sel{index}", "tell me more", selected_product_id=product_id)
            self.assertEqual(r.status_code, 400)
            bodies.append(r.content)
        self.assertEqual(len(set(bodies)), 1)
        ok = self.send(s, "own", "what colors does this come in?", selected_product_id="jacket-a")
        _, body = self.result(s, ok)
        self.assertEqual(body["selected_product_id"], "jacket-a")

    def test_idempotent_retry_and_payload_binding(self):
        s = self.start()
        first = self.send(s, "same", "rain jacket under 150 dollars")
        calls = self.decisions
        again = self.send(s, "same", "rain jacket under 150 dollars")
        self.assertEqual(first.text, again.text)
        self.assertEqual(self.decisions, calls)
        changed = self.send(s, "same", "rain jacket under 100 dollars")
        self.assertEqual(changed.status_code, 409)
        session = self.app.state.admission.sessions[s["session_id"]]
        self.assertEqual((session.turn_count, len(session.conversation.exchanges)), (1, 1))

    def test_model_supplied_urls_and_paths_never_become_assets(self):
        class Injecting(MockBackend):
            script = [
                Decision(
                    tool="get_product_details",
                    arguments={"product_id": "https://evil.example/x.png"},
                    call_id="c1",
                ),
                Decision(
                    tool="get_product_details",
                    arguments={"product_id": "../../etc/passwd"},
                    call_id="c2",
                ),
                Decision(answer="See https://evil.example/x.png and /etc/passwd for photos."),
            ]

            async def decide(self, messages, tools, tool_choice="auto"):
                return self.script.pop(0)

        app = create_app(KEYS, OPERATOR, backend=Injecting())
        with TestClient(app) as client:
            self.client = client
            s = self.start()
            events, body = self.result(s, self.send(s, "inject", "show me jackets"))
        denied = [e["reason"] for e in events if e["event_type"] == "tool.denied"]
        self.assertEqual(denied, ["tool_arguments_denied", "tool_arguments_denied"])
        self.assertEqual((body["product_cards"], body["recommended_product_ids"]), ([], []))
        self.assertNotIn("evil", json.dumps(body["product_cards"]))

    def test_info_names_the_configured_stages_before_sign_in(self):
        """the architecture view reads before sign-in, so /v1/info (no key) carries the
        configured model of each stage, the profile digest and the context budget. Nothing else."""
        info = self.client.get("/v1/info").json()
        self.assertEqual(
            info["stages"],
            {
                "asr": {"model_id": "mock-asr", "model_revision": "fixture-v1"},
                "model": {"model_id": "mock-model", "model_revision": "fixture-v1"},
                "tts": {"model_id": "mock-tts", "model_revision": "fixture-v1"},
            },
        )
        self.assertEqual(
            (info["profile_digest"], info["context_tokens"]), ("mock-fixture-v1", 16384)
        )
        self.assertEqual(
            set(info),
            {
                "mode",
                "site",
                "region",
                "pipeline",
                "fixture_enabled",
                "profile_digest",
                "context_tokens",
                "stages",
                "walkthrough",
                "tryon",
            },
        )
        self.assertIs(info["walkthrough"], False)
        text = json.dumps(info)
        for secret in (*KEYS, OPERATOR):
            self.assertNotIn(secret, text)

    def test_tools_and_the_turn_carry_gateway_durations(self):
        """The timings table shows each tool's duration and the gateway's turn total, both on the
        gateway's clock. A denied lookup is timed too."""
        s = self.start()
        events, _ = self.result(
            s, self.send(s, "t0", "I need a lightweight jacket for rainy weather under 150 dollars")
        )
        tools = [e for e in events if e["event_type"] == "tool.completed"]
        self.assertTrue(tools)
        self.assertTrue(all(e["duration_ms"] >= 0 for e in tools))
        self.assertEqual(events[-1]["event_type"], "turn.completed")
        stages = sum(e.get("duration_ms", 0) for e in events if e["event_type"] != "turn.completed")
        self.assertGreaterEqual(events[-1]["duration_ms"], 0)
        self.assertLessEqual(stages, events[-1]["duration_ms"] + 1)
        events, _ = self.result(s, self.send(s, "t1", "Look up product id jacket-b please"))
        denied = [e for e in events if e["event_type"] == "tool.denied"]
        self.assertTrue(denied and all(e["duration_ms"] >= 0 for e in denied))

    def test_turn_end_names_the_reply_audio_for_a_parallel_fetch(self):
        """turn.completed carries audio_path and audio_kind, the same route the result names,
        so the browser fetches the audio with the result. A failed turn names no audio."""
        s = self.start()
        events, body = self.result(s, self.send(s, "a0", "Hi there"))
        terminal = events[-1]
        self.assertEqual(terminal["event_type"], "turn.completed")
        self.assertEqual(terminal["audio_path"], body["audio_path"])
        self.assertEqual((terminal["audio_kind"], body["audio_kind"]), ("test_tone", "test_tone"))
        h = {"Authorization": "Bearer " + s["session_token"]}
        self.assertEqual(self.client.get(terminal["audio_path"], headers=h).status_code, 200)
        self.assertEqual(self.client.get(terminal["audio_path"]).status_code, 401)
        self.assertTrue(all("audio_path" not in e for e in events[:-1]))

        class Broken(MockBackend):
            async def decide(self, messages, tools, tool_choice="auto"):
                raise RuntimeError("model down")

        # A second gateway whose model fails, for one failed turn.
        app = create_app(KEYS, OPERATOR, backend=Broken())
        with TestClient(app) as client:
            setup = client.post(
                "/v1/sessions",
                headers={"Authorization": "Bearer tenant-b-test-key-0002", "Idempotency-Key": "b"},
                json={},
            ).json()
            response = client.post(
                f"/v1/sessions/{setup['session_id']}/messages",
                headers={
                    "Authorization": "Bearer " + setup["session_token"],
                    "Idempotency-Key": "f0",
                },
                files={"text": (None, "Hi there")},
            )
            failed = [json.loads(line) for line in response.text.splitlines()][-1]
        self.assertEqual(failed["event_type"], "turn.failed")
        self.assertNotIn("audio_path", failed)
        jsonschema.validate(failed, self.event_schema)

    def test_failed_message_result_matches_the_schema(self):
        self.backend.context_tokens = 2048
        s = self.start()
        events, body = self.result(s, self.send(s, "small", "rain jacket"))
        self.assertEqual((body["status"], body["reason"]), ("failed", "context_limit"))
        self.assertEqual(events[-1]["event_type"], "turn.failed")
        self.assertIsNone(body["audio_path"])
