import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from retail_voice.admission import State
from retail_voice.api import create_app, fixture_media
from retail_voice.cli import preflight, vlm_command
from retail_voice.models.mock import MockBackend
from retail_voice.models.nvidia import ModelRejected, NvidiaBackend, validate_reply_audio
from retail_voice.models.tools import LEGACY_INVENTORY_TOOL
from retail_voice.pipeline import run_turn
from retail_voice.prompts import legacy_messages, tool_exchange
from retail_voice.settings import GPUSettings, model_manifest

KEYS = {"nvidia-test-key-tenant-a": "retailer-a", "nvidia-test-key-tenant-b": "retailer-b"}
OPERATOR = "nvidia-test-operator-key"


class NvidiaTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_speech_preserves_every_sentence(self):
        text = "This is a complete sentence about a jacket. " * 40
        audio = await self.backend.synthesize(text)
        sent = [json.loads(r.content)["text"] for r in self.requests if r.url.path == "/tts"]
        self.assertGreater(len(sent), 1)
        self.assertEqual(" ".join(sent), text.strip())
        self.assertTrue(all(len(part) <= 1200 for part in sent))
        self.assertGreater(len(audio), len(self.wav))
        validate_reply_audio(audio)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = GPUSettings(
            site_id="site-a",
            region="us-test",
            model_root=Path(self.temp.name) / "models",
            runtime_dir=Path(self.temp.name) / "runtime",
        )
        self.settings.runtime_dir.mkdir()
        self.requests = []
        self.chat_count = 0
        self.wav = await MockBackend().synthesize("synthetic contract-test audio")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.backend = NvidiaBackend(self.settings, client=self.client)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "busy": False,
                    "device": "cuda",
                    "profile_digest": self.settings.profile_digest,
                },
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "retail-nemotron"}]})
        if request.url.path == "/asr":
            return httpx.Response(
                200, json={"text": "Does the jacket have a hood and is navy medium in stock?"}
            )
        if request.url.path == "/tts":
            return httpx.Response(200, content=self.wav)
        if request.url.path == "/v1/chat/completions":
            self.chat_count += 1
            payload = json.loads(request.content)
            if payload["tool_choice"] == "auto":
                selected = json.loads(payload["messages"][1]["content"][0]["text"])
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "lookup_inventory",
                                                "arguments": json.dumps(
                                                    {
                                                        "sku": selected["selected_sku"],
                                                        "store_id": selected["selected_store"],
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "It has a hood. There are 3 navy medium jackets."
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    async def test_complete_nvidia_protocol_uses_image_tool_result_and_pinned_labels(self):
        self.assertTrue(await self.backend.ready())
        s = State().reserve("retailer-a", "setup")
        audio, image = fixture_media()
        r = await run_turn(self.backend, s, audio, image, "jacket-a", "store-a")
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["tool_result"]["navy_medium"], 3)
        self.assertNotIn("hood", r["tool_result"])
        self.assertEqual(self.chat_count, 2)
        self.assertFalse(self.settings.quarantine_path.exists())
        chat = [
            json.loads(req.content)
            for req in self.requests
            if req.url.path == "/v1/chat/completions"
        ]
        self.assertEqual(
            chat[0]["messages"][1]["content"][1]["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(image).decode(),
        )
        self.assertEqual(chat[1]["messages"][-1]["tool_call_id"], "call-1")
        self.assertNotIn("hood", chat[1]["messages"][-1]["content"])
        self.assertEqual(chat[0]["chat_template_kwargs"], {"enable_thinking": False})
        for e in r["events"]:
            self.assertEqual(e["site_id"], "site-a")
            self.assertEqual(e["mode"], "nvidia")
            self.assertEqual(e["pipeline"], "pipecat")
            self.assertNotIn("transcript", e)
        self.assertTrue(all(req.url.host == "127.0.0.1" for req in self.requests))
        model_events = [e for e in r["events"] if e["event_type"] == "model.completed"]
        self.assertEqual(
            model_events[0]["model_revision"], model_manifest()["models"]["vlm"]["revision"]
        )

    async def test_foreign_model_tool_call_stops_before_tts(self):
        s = State().reserve("retailer-a", "setup")
        r = await run_turn(self.backend, s, *fixture_media(), "jacket-b", "store-b")
        self.assertEqual(r["reason"], "catalog_scope_denied")
        self.assertNotIn("/tts", [req.url.path for req in self.requests])
        self.assertTrue(any(e["event_type"] == "tool.denied" for e in r["events"]))

    async def test_timeout_quarantines_backend_across_gateway_restart(self):
        async def timeout(request):
            raise httpx.ReadTimeout("sensitive transport detail")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        self.backend.client = self.client
        with self.assertRaises(httpx.ReadTimeout):
            await self.backend.transcribe(fixture_media()[0])
        self.assertTrue(self.settings.quarantine_path.exists())
        self.assertFalse(await self.backend.ready())
        restarted = NvidiaBackend(self.settings, client=self.client)
        self.assertFalse(await restarted.ready())
        self.assertNotIn("sensitive", self.settings.quarantine_path.read_text())

    async def test_marker_is_durable_before_request_and_blocks_restart_during_inference(self):
        async def observing(request):
            self.assertTrue(self.settings.quarantine_path.exists())
            restarted = NvidiaBackend(self.settings, client=self.client)
            self.assertFalse(await restarted.ready())
            return self.handle(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(observing))
        self.backend.client = self.client
        await self.backend.transcribe(fixture_media()[0])
        self.assertFalse(self.settings.quarantine_path.exists())

    async def test_missing_runtime_directory_cannot_send_inference(self):
        self.settings.runtime_dir.rmdir()
        with self.assertRaises(OSError):
            await self.backend.transcribe(fixture_media()[0])
        self.assertEqual(self.requests, [])
        self.assertTrue(self.backend.quarantined)

    async def test_cancel_quarantines_ambiguous_inference(self):
        started = asyncio.Event()

        async def slow(request):
            started.set()
            await asyncio.sleep(60)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        self.backend.client = self.client
        task = asyncio.create_task(self.backend.transcribe(fixture_media()[0]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(await self.backend.ready())

    async def test_redirect_is_not_followed_and_quarantines(self):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    307, headers={"Location": "https://outside.invalid/asr"}
                )
            )
        )
        self.backend.client = self.client
        with self.assertRaises(RuntimeError):
            await self.backend.transcribe(fixture_media()[0])
        self.assertTrue(self.backend.quarantined)

    async def test_response_limit_quarantines(self):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 131073)
            )
        )
        self.backend.client = self.client
        with self.assertRaises(ValueError):
            await self.backend.transcribe(fixture_media()[0])
        self.assertTrue(self.backend.quarantined)

    async def test_invalid_tool_arguments_do_not_reach_catalog(self):
        def bad(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "bad",
                                        "type": "function",
                                        "function": {"name": "lookup_inventory", "arguments": "[]"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(bad))
        self.backend.client = self.client
        messages = legacy_messages("question", fixture_media()[1], "jacket-a", "store-a")
        decision = await self.backend.decide(messages, [LEGACY_INVENTORY_TOOL])
        # A non-object argument string reaches the loop as "no arguments", never the catalog.
        self.assertEqual((decision.tool, decision.arguments), ("lookup_inventory", None))

    async def test_answer_without_catalog_call_fails(self):
        def made_up(request):
            self.requests.append(request)
            if request.url.path == "/asr":
                return httpx.Response(200, json={"text": "Is navy medium in stock?"})
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": "Made up"}}]},
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(made_up))
        self.backend.client = self.client
        # The diagnostic turn requires the inventory tool before any answer.
        s = State().reserve("retailer-a", "setup")
        r = await run_turn(self.backend, s, *fixture_media(), "jacket-a", "store-a")
        self.assertEqual((r["status"], r["reason"]), ("failed", "backend_failed"))
        self.assertNotIn("/tts", [req.url.path for req in self.requests])
        self.assertFalse(self.settings.quarantine_path.exists())

    async def test_model_markup_is_not_displayed_or_synthesized(self):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "<think>hidden</think> answer"},
                            }
                        ]
                    },
                )
            )
        )
        self.backend.client = self.client
        messages = legacy_messages("question", fixture_media()[1], "jacket-a", "store-a")
        messages += tool_exchange("call-1", "lookup_inventory", {}, {})
        decision = await self.backend.decide(messages, [LEGACY_INVENTORY_TOOL], "none")
        # Withheld, not parsed: no answer, no tool, nothing of the text survives.
        self.assertEqual(decision.malformed, "unexpected_model_markup")
        self.assertIsNone(decision.text)
        self.assertIsNone(decision.tool)
        self.assertNotIn("hidden", repr(decision))

    async def test_vlm_400_is_a_definitive_refusal_without_quarantine(self):
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": {"message": "too long"}})
            )
        )
        self.backend.client = self.client
        messages = legacy_messages("question", fixture_media()[1], "jacket-a", "store-a")
        with self.assertRaises(ModelRejected):
            await self.backend.decide(messages, [LEGACY_INVENTORY_TOOL])
        self.assertFalse(self.settings.quarantine_path.exists())
        self.assertFalse(self.backend.quarantined)
        self.assertEqual(self.backend.inflight, 0)

    async def test_readiness_rejects_wrong_profile_and_busy_speech(self):
        for changes in [{"profile_digest": "wrong"}, {"busy": True}, {"device": "cpu"}]:

            def wrong(request):
                if request.url.path == "/health":
                    return httpx.Response(
                        200,
                        json={
                            "ready": True,
                            "busy": False,
                            "device": "cuda",
                            "profile_digest": self.settings.profile_digest,
                        }
                        | changes,
                    )
                return self.handle(request)

            self.client = httpx.AsyncClient(transport=httpx.MockTransport(wrong))
            self.backend.client = self.client
            self.assertFalse(await self.backend.ready())
            await self.client.aclose()

    async def test_preflight_reports_missing_hardware_without_claiming_validation(self):
        with (
            patch("retail_voice.cli.gpu_info", side_effect=FileNotFoundError),
            patch("retail_voice.cli.NvidiaBackend", return_value=self.backend),
        ):
            report = await preflight(self.settings)
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["hardware_validated"])
        self.assertFalse(report["checks"]["pinned_artifacts"])

    async def test_vlm_command_uses_pins_loopback_and_single_request_limits(self):
        command = vlm_command(self.settings)
        self.assertIn(model_manifest()["runtime"]["vllm_image"], command)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--max-num-seqs") + 1], "1")
        self.assertNotIn("--allowed-local-media-path", command)
        self.assertIn("HF_HUB_OFFLINE=1", command)
        self.assertEqual(command[command.index("--max-model-len") + 1], "4096")
        trial = self.settings.model_copy(update={"context_tokens": 16384})
        trial_command = vlm_command(trial)
        self.assertEqual(trial_command[trial_command.index("--max-model-len") + 1], "16384")
        self.assertEqual(trial_command[trial_command.index("--max-num-seqs") + 1], "1")
        self.assertEqual(trial_command[trial_command.index("--kv-cache-dtype") + 1], "fp8")
        # The context limit is part of profile identity.
        self.assertNotEqual(trial.profile_digest, self.settings.profile_digest)

    async def test_context_tokens_bounds(self):
        base = dict(site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r")
        self.assertEqual(GPUSettings(**base).context_tokens, 4096)
        self.assertEqual(GPUSettings(**base, context_tokens=16384).context_tokens, 16384)
        for bad in (1024, 65536, 16384.5):
            with self.assertRaises(ValueError):
                GPUSettings.model_validate_json(json.dumps(base | {"context_tokens": bad}))

    async def test_busy_speech_is_healthy_for_admission_but_not_ready(self):
        def busy(request):
            if request.url.path == "/health":
                return httpx.Response(
                    200,
                    json={
                        "ready": True,
                        "busy": True,
                        "device": "cuda",
                        "profile_digest": self.settings.profile_digest,
                    },
                )
            return self.handle(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(busy))
        self.backend.client = self.client
        self.assertFalse(await self.backend.ready())
        self.assertTrue(await self.backend.healthy())
        # A leftover marker from another process is quarantine, not busy.
        self.settings.quarantine_path.write_text("{}")
        self.assertFalse(await self.backend.healthy())
        self.assertFalse(await NvidiaBackend(self.settings, client=self.client).healthy())

    async def test_reply_wav_validation(self):
        self.assertEqual(validate_reply_audio(self.wav), self.wav)
        for data in [b"not audio", self.wav[:60], b"x" * 8_000_001]:
            with self.assertRaises((ValueError, EOFError)):
                validate_reply_audio(data)


class NvidiaAPITests(unittest.TestCase):
    def test_runtime_info_fixture_gating_and_authenticated_readiness(self):
        class FakeGPU(MockBackend):
            mode, site_id, region = "nvidia", "site-a", "us-test"

        with patch.dict(os.environ, {"DEMO_MODE": "nvidia"}):
            with TestClient(create_app(KEYS, OPERATOR, backend=FakeGPU())) as client:
                self.assertEqual(client.get("/v1/info").json()["mode"], "nvidia")
                self.assertEqual(client.get("/livez").json()["mode"], "nvidia")
                self.assertEqual(client.post("/v1/sessions/any/fixture-turn").status_code, 404)
                self.assertEqual(client.get("/admin/ready").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/admin/ready", headers={"Authorization": "Bearer " + OPERATOR}
                    ).status_code,
                    200,
                )

    def test_unready_backend_fails_startup_without_fallback(self):
        class Unready(MockBackend):
            async def ready(self):
                return False

        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            with self.assertRaises(RuntimeError):
                with TestClient(create_app(KEYS, OPERATOR, backend=Unready())):
                    pass

    def test_settings_reject_placeholders_and_url_override(self):
        with self.assertRaises(ValueError):
            GPUSettings(
                site_id="site-a",
                region="REPLACE_WITH_REGION",
                model_root="/models",
                runtime_dir="/runtime",
            )
        with self.assertRaises(ValueError):
            GPUSettings(
                site_id="site-a",
                region="us-test",
                model_root="/models",
                runtime_dir="/runtime",
                speech_url="https://outside.invalid",
            )


class NvidiaShoppingProtocolTests(unittest.IsolatedAsyncioTestCase):
    """The shopping loop over the real NVIDIA adapter, with fake loopback services."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = GPUSettings(
            site_id="site-a",
            region="us-test",
            model_root=Path(self.temp.name) / "models",
            runtime_dir=Path(self.temp.name) / "runtime",
            context_tokens=16384,
        )
        self.settings.runtime_dir.mkdir()
        self.chats, self.spoken = [], []
        self.wav = await MockBackend().synthesize("contract audio")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.backend = NvidiaBackend(self.settings, client=self.client)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def handle(self, request):
        if request.url.path == "/tts":
            self.spoken.append(json.loads(request.content)["text"])
            return httpx.Response(200, content=self.wav)
        payload = json.loads(request.content)
        self.chats.append(payload)
        if not any(m["role"] == "tool" for m in payload["messages"]):
            call = {
                "id": "chatcmpl-tool-1",
                "type": "function",
                "function": {
                    "name": "search_products",
                    "arguments": json.dumps(
                        {"query": "lightweight rain jacket", "max_price_minor": 15000}
                    ),
                },
            }
            message = {"content": None, "tool_calls": [call]}
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]}
            )
        message = {"content": "The Drizzle Anorak at $69 is the lightest option."}
        usage = {"prompt_tokens": 2112, "completion_tokens": 14, "total_tokens": 2126}
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": message}], "usage": usage},
        )

    async def test_text_message_searches_then_answers_over_vllm(self):
        from retail_voice.catalog_repository import default_repository
        from retail_voice.conversation import Conversation
        from retail_voice.pipeline import Message, run_message

        catalog = default_repository()
        state = State()
        s = state.reserve("retailer-a", "setup")
        s.conversation = Conversation("retailer-a", "store-a")
        text = "I need a lightweight jacket for rainy weather under 150 dollars"
        r = await run_message(self.backend, s, Message(text=text), catalog, deadline=10)
        self.assertEqual(r["status"], "complete")
        self.assertEqual(
            [c["product_id"] for c in r["product_cards"]],
            ["a-drizzle-anorak", "a-downpour-packable", "jacket-a"],
        )
        self.assertEqual(
            r["recommended_product_ids"], [p["product_id"] for p in r["product_cards"]]
        )
        self.assertEqual(len(self.chats), 2)
        first, second = self.chats
        self.assertEqual(
            [t["function"]["name"] for t in first["tools"]],
            ["search_products", "get_product_details", "check_availability", "add_to_cart"],
        )
        stores = first["tools"][2]["function"]["parameters"]["properties"]["store_id"]["enum"]
        self.assertEqual(stores, ["store-a", "store-a-riverside"])
        self.assertEqual((first["tool_choice"], second["tool_choice"]), ("auto", "auto"))
        current = json.loads(first["messages"][-1]["content"])
        self.assertEqual((current["shopper_message"], current["displayed_products"]), (text, []))
        self.assertNotIn("image_url", json.dumps(first["messages"]))
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertEqual(second["messages"][-1]["tool_call_id"], "chatcmpl-tool-1")
        self.assertNotIn("Harbor", json.dumps(second["messages"]))
        # The screen keeps "$69"; the speech service gets the spoken form.
        self.assertEqual(
            r["answer"],
            "I found the Drizzle Anorak, Downpour Packable Shell and Ridgeline Rain Shell within your budget. Which one would you like?",
        )
        self.assertEqual(self.spoken, [r["answer"]])
        self.assertFalse(self.settings.quarantine_path.exists())
        call = next(e for e in r["events"] if e["event_type"] == "tool.completed")
        self.assertEqual(call["tool_call_id"], "chatcmpl-tool-1")
        models = [e for e in r["events"] if e["event_type"] == "model.completed"]
        self.assertNotIn("prompt_tokens", models[0])
        self.assertEqual((models[1]["prompt_tokens"], models[1]["completion_tokens"]), (2112, 14))
        self.assertNotIn("total_tokens", models[1])

    async def test_markup_reply_gets_one_plain_words_retry_then_fails(self):
        # On the reference GPU host a cornered model (four denied tool calls, then tool_choice "none")
        # wrote a tool call as text, and the turn failed as backend_failed. The text is
        # withheld and the model gets one retry with a hint; a second such reply fails.
        from retail_voice.catalog_repository import default_repository
        from retail_voice.conversation import Conversation
        from retail_voice.pipeline import MALFORMED_HINT, Message, run_message

        chats = []
        replies = ["<tool_call>{}</tool_call>", "I could not confirm that. Which color?"]

        def handle(request):
            if request.url.path == "/tts":
                return httpx.Response(200, content=self.wav)
            chats.append(json.loads(request.content))
            message = {"content": replies[min(len(chats) - 1, len(replies) - 1)]}
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": message}]}
            )

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.backend = NvidiaBackend(self.settings, client=self.client)
        catalog = default_repository()
        state = State()
        s = state.reserve("retailer-a", "setup")
        s.conversation = Conversation("retailer-a", "store-a")
        r = await run_message(
            self.backend, s, Message(text="yes that is the one"), catalog, deadline=10
        )
        self.assertEqual(r["status"], "complete")
        self.assertEqual(r["answer"], "I could not confirm that. Which color?")
        self.assertEqual(len(chats), 2)
        self.assertEqual(
            chats[1]["messages"][-1], {"role": "user", "content": MALFORMED_HINT["auto"]}
        )
        self.assertNotIn("tool_call>", json.dumps(chats[1]["messages"]))
        kinds = [e["decision"] for e in r["events"] if e["event_type"] == "model.completed"]
        self.assertEqual(kinds, ["malformed", "answer"])
        self.assertFalse(self.settings.quarantine_path.exists())

        # Markup twice: the turn fails without a fallback answer and nothing is spoken.
        chats.clear()
        replies[:] = ["<think>plan</think>", "</think> still tagged"]
        s = state.reserve("retailer-b", "setup-b")
        s.conversation = Conversation("retailer-b", "store-b")
        r = await run_message(self.backend, s, Message(text="hello"), catalog, deadline=10)
        self.assertEqual((r["status"], r["reason"]), ("failed", "backend_failed"))
        self.assertEqual(len(chats), 2)
        self.assertNotIn("plan", json.dumps(r["events"]) + str(r.get("answer")))
        kinds = [e["decision"] for e in r["events"] if e["event_type"] == "model.completed"]
        self.assertEqual(kinds, ["malformed", "malformed"])

    async def test_malformed_tool_arguments_are_reported_back_and_the_turn_completes(self):
        from retail_voice.catalog_repository import default_repository
        from retail_voice.conversation import Conversation
        from retail_voice.pipeline import Message, run_message

        chats = []

        def tool_call(call_id, arguments):
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": "get_product_details", "arguments": arguments},
            }
            message = {"content": None, "tool_calls": [call]}
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]}
            )

        def handle(request):
            if request.url.path == "/tts":
                return httpx.Response(200, content=self.wav)
            chats.append(json.loads(request.content))
            if len(chats) == 1:
                return tool_call("chatcmpl-tool-1", '{"product_id": ')
            if len(chats) == 2:
                return tool_call("chatcmpl-tool-2", json.dumps({"product_id": "a-drizzle-anorak"}))
            message = {"content": "The Drizzle Anorak comes in green and navy."}
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": message}]}
            )

        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.backend.client = self.client
        state = State()
        s = state.reserve("retailer-a", "setup")
        s.conversation = Conversation("retailer-a", "store-a")
        message = Message(text="tell me about the drizzle anorak")
        r = await run_message(self.backend, s, message, default_repository(), deadline=10)
        self.assertEqual(r["status"], "complete")
        kinds = [e["event_type"] for e in r["events"]]
        self.assertEqual((kinds.count("tool.denied"), kinds.count("tool.completed")), (1, 1))
        denied = next(e for e in r["events"] if e["event_type"] == "tool.denied")
        self.assertEqual(denied["reason"], "tool_arguments_denied")
        echoed, reply = chats[1]["messages"][-2], chats[1]["messages"][-1]
        self.assertEqual(json.loads(echoed["tool_calls"][0]["function"]["arguments"]), {})
        self.assertEqual(reply["role"], "tool")
        self.assertIn("invalid_arguments", reply["content"])
        self.assertEqual(r["selected_product_id"], "a-drizzle-anorak")
        self.assertFalse(self.settings.quarantine_path.exists())


class ConversationSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_smoke_runs_against_a_fake_gpu_gateway(self):
        """The smoke script itself, in process. A GPU run is still required for evidence."""
        from retail_voice.cli import conversation_smoke

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = GPUSettings(
            site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r"
        )

        class FakeGPU(MockBackend):
            mode, site_id, region = "nvidia", "site-a", "us-test"
            profile_digest = settings.profile_digest

        env = {
            "DEMO_MODE": "nvidia",
            "TENANT_A_KEY": "nvidia-test-key-tenant-a",
            "TENANT_B_KEY": "nvidia-test-key-tenant-b",
        }
        with patch.dict(os.environ, env):
            app = create_app(KEYS, OPERATOR, backend=FakeGPU())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                report = await conversation_smoke(
                    client, settings, fixture_media()[0], save_replies=temp.name
                )
        self.assertEqual(report["hard_failures"], [])
        self.assertEqual(report["shopping_failures"], [])
        self.assertTrue(report["runtime_checks_passed"])
        self.assertTrue(report["shopping_checks_passed"])
        self.assertEqual(len(report["turns"]), 23)
        self.assertEqual(report["fresh_session"]["status"], "complete")
        small = [t for t in report["turns"] if t["step"] in ("greeting", "date")]
        self.assertEqual(len(small), 4)
        self.assertTrue(all(t["tools"] == [] and t["status"] == "complete" for t in small))
        foreign = [t for t in report["turns"] if t["step"] == "foreign_attempt"]
        self.assertEqual([t["foreign_outcome"] for t in foreign], ["denied_by_catalog"] * 2)
        self.assertEqual(report["expectations"]["missed"], [])
        unavailable = [t for t in report["turns"] if t["step"] == "unavailable"]
        self.assertEqual(
            sorted(t["stock_checks"][0]["variant_id"] for t in unavailable),
            ["a-stormline-pro-shell-red-m", "jacket-b-red-m"],
        )
        text = json.dumps(report)
        for secret in env.values():
            self.assertNotIn(secret, text)
        self.assertNotIn("Fixture answer", text)
        self.assertNotIn("lightweight jacket", text)
        self.assertEqual(len(list(Path(temp.name).glob("*.wav"))), 23)

    async def test_budget_wording_swaps_only_the_changed_budget_step(self):
        """Alternative budget wording changes one step and preserves the rest of the smoke."""
        from retail_voice.cli import CONVERSATION, conversation_smoke, conversation_steps

        self.assertIs(conversation_steps("default"), CONVERSATION)
        for wording, sentence in (
            ("keep", "Let's keep it under 100 dollars."),
            ("anything", "Anything under 100?"),
        ):
            steps = conversation_steps(wording)
            for tenant in ("a", "b"):
                changed = [s for s in steps[tenant] if s[0] == "changed_budget"]
                self.assertEqual([(s[1], s[2]["max_price"]) for s in changed], [(sentence, 10000)])
                others = [s for s in steps[tenant] if s[0] != "changed_budget"]
                self.assertEqual(
                    others, [s for s in CONVERSATION[tenant] if s[0] != "changed_budget"]
                )
        settings = GPUSettings(
            site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r"
        )

        class FakeGPU(MockBackend):
            mode, site_id, region = "nvidia", "site-a", "us-test"
            profile_digest = settings.profile_digest

        env = {
            "DEMO_MODE": "nvidia",
            "TENANT_A_KEY": "nvidia-test-key-tenant-a",
            "TENANT_B_KEY": "nvidia-test-key-tenant-b",
        }
        for wording in ("keep", "anything"):
            with self.subTest(wording=wording), patch.dict(os.environ, env):
                app = create_app(KEYS, OPERATOR, backend=FakeGPU())
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    report = await conversation_smoke(
                        client, settings, fixture_media()[0], budget_wording=wording
                    )
                self.assertEqual(report["budget_wording"], wording)
                self.assertEqual(report["hard_failures"], [])
                self.assertEqual(report["shopping_failures"], [])
                budget = [t for t in report["turns"] if t["step"] == "changed_budget"]
                self.assertEqual(len(budget), 2)
                self.assertTrue(
                    all(any(x["name"] == "search_products" for x in t["tools"]) for t in budget)
                )
                self.assertNotIn("keep it under", json.dumps(report))

    async def test_a_cart_change_outside_the_add_step_fails_the_shopping_gate(self):
        """the gateway refuses unrequested adds; the smoke also fails if one ever lands."""
        from retail_voice.cli import CONVERSATION, conversation_smoke

        settings = GPUSettings(
            site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r"
        )

        class FakeGPU(MockBackend):
            mode, site_id, region = "nvidia", "site-a", "us-test"
            profile_digest = settings.profile_digest

        env = {
            "DEMO_MODE": "nvidia",
            "TENANT_A_KEY": "nvidia-test-key-tenant-a",
            "TENANT_B_KEY": "nvidia-test-key-tenant-b",
        }
        steps = {
            "a": [next(step for step in CONVERSATION["a"] if step[0] == "greeting")],
            "b": [next(step for step in CONVERSATION["b"] if step[0] == "date")],
        }

        class AddedUnasked(httpx.ASGITransport):
            """Reports an add_to_cart on the greeting, as a model that slipped past would."""

            async def handle_async_request(self, request):
                body = await request.aread()
                response = await super().handle_async_request(request)
                if not request.url.path.endswith("/messages") or b"Hi there" not in body:
                    return response
                events = [json.loads(line) for line in (await response.aread()).splitlines()]
                added = events[-1] | {
                    "event_type": "tool.completed",
                    "status": "complete",
                    "tool_name": "add_to_cart",
                    "tool_call_id": "call-1",
                }
                added.pop("result_path")
                body = "".join(json.dumps(e) + "\n" for e in events[:-1] + [added, events[-1]])
                return httpx.Response(200, content=body.encode())

        with patch.dict(os.environ, env), patch("retail_voice.cli.CONVERSATION", steps):
            app = create_app(KEYS, OPERATOR, backend=FakeGPU())
            async with httpx.AsyncClient(
                transport=AddedUnasked(app=app), base_url="http://test"
            ) as client:
                report = await conversation_smoke(client, settings, fixture_media()[0])
        self.assertEqual(report["shopping_failures"], ["a:greeting:cart_unchanged"])
        self.assertEqual(report["hard_failures"], [])
        self.assertFalse(report["shopping_checks_passed"])

    async def test_small_talk_tool_calls_are_reported_but_not_shopping_failures(self):
        from retail_voice.cli import CONVERSATION, conversation_smoke

        settings = GPUSettings(
            site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r"
        )

        class Chatty(MockBackend):
            """Searches on a greeting, as a model might."""

            mode, site_id, region = "nvidia", "site-a", "us-test"
            profile_digest = settings.profile_digest

            def small_talk(self, messages, context, text):
                if text == "hi there":
                    return self.call([], "search_products", {"query": "jacket"})
                return super().small_talk(messages, context, text)

        env = {
            "DEMO_MODE": "nvidia",
            "TENANT_A_KEY": "nvidia-test-key-tenant-a",
            "TENANT_B_KEY": "nvidia-test-key-tenant-b",
        }
        steps = {
            "a": [next(step for step in CONVERSATION["a"] if step[0] == "greeting")],
            "b": [next(step for step in CONVERSATION["b"] if step[0] == "date")],
        }
        with patch.dict(os.environ, env), patch("retail_voice.cli.CONVERSATION", steps):
            app = create_app(KEYS, OPERATOR, backend=Chatty())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                report = await conversation_smoke(client, settings, fixture_media()[0])
        self.assertEqual(report["expectations"]["missed"], ["a:greeting:small_talk_without_tools"])
        self.assertEqual(report["shopping_failures"], [])
        self.assertEqual(report["hard_failures"], [])
        self.assertTrue(report["shopping_checks_passed"])

    async def test_stock_acceptance_fails_independently_of_runtime_health(self):
        from retail_voice.cli import CONVERSATION, conversation_smoke

        settings = GPUSettings(
            site_id="site-a", region="us-test", model_root="/m", runtime_dir="/r"
        )

        class FakeGPU(MockBackend):
            mode, site_id, region = "nvidia", "site-a", "us-test"
            profile_digest = settings.profile_digest

        env = {
            "DEMO_MODE": "nvidia",
            "TENANT_A_KEY": "nvidia-test-key-tenant-a",
            "TENANT_B_KEY": "nvidia-test-key-tenant-b",
        }
        steps = {
            "a": [CONVERSATION["a"][0]],
            "b": [next(step for step in CONVERSATION["b"] if step[0] == "unavailable")],
        }
        mutations = {
            "product": lambda data: data["stock_checks"][0].update(
                product_id="b-squall-rain-shell"
            ),
            "variant": lambda data: data["stock_checks"][0].update(variant_id="jacket-b-navy-m"),
            "store": lambda data: data["stock_checks"][0].update(store_id="store-a"),
            "quantity": lambda data: data["stock_checks"][0].update(quantity=7),
            "missing": lambda data: data.update(stock_checks=[]),
            "selection": lambda data: data.update(selected_product_id="b-squall-rain-shell"),
            "prose": lambda data: data.update(answer="The Squall Rain Shell is in stock."),
        }
        for name, mutate in mutations.items():
            with (
                self.subTest(name=name),
                patch.dict(os.environ, env),
                patch("retail_voice.cli.CONVERSATION", steps),
            ):
                app = create_app(KEYS, OPERATOR, backend=FakeGPU())

                class CorruptResult(httpx.ASGITransport):
                    async def handle_async_request(self, request):
                        response = await super().handle_async_request(request)
                        if request.url.path.endswith("/result"):
                            body = json.loads(await response.aread())
                            if body.get("stock_checks"):
                                mutate(body)
                                return httpx.Response(200, json=body)
                        return response

                async with httpx.AsyncClient(
                    transport=CorruptResult(app=app), base_url="http://test"
                ) as client:
                    report = await conversation_smoke(client, settings, fixture_media()[0])
                self.assertEqual(report["hard_failures"], [])
                self.assertTrue(report["runtime_checks_passed"])
                self.assertFalse(report["shopping_checks_passed"])
                self.assertTrue(report["shopping_failures"])
