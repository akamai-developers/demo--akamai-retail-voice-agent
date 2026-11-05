"""tools/measure_latency.py against the mock gateway, in process. A GPU run is separate evidence."""

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from retail_voice.api import create_app, fixture_media
from retail_voice.models.mock import MockBackend

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"tenant-a-test-key-0001": "retailer-a", "tenant-b-test-key-0002": "retailer-b"}
OPERATOR = "operator-test-key-0003"
spec = importlib.util.spec_from_file_location("measure_latency", ROOT / "tools/measure_latency.py")
assert spec and spec.loader
measure_latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure_latency)


class MeasureLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, trials=2, audio_fetch="auto"):
        app = create_app(KEYS, OPERATOR, backend=MockBackend())
        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                info, turns = await measure_latency.measure(
                    client, "tenant-a-test-key-0001", fixture_media()[0], trials, audio_fetch
                )
                # Every trial signed out: the tenant can reserve again at once.
                again = await client.post(
                    "/v1/sessions",
                    headers={
                        "Authorization": "Bearer tenant-a-test-key-0001",
                        "Idempotency-Key": "after",
                    },
                    json={},
                )
        return info, turns, again

    async def test_fixed_workload_timed_on_both_clocks_without_text(self):
        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            info, turns, again = await self.run_tool()
        self.assertEqual(again.status_code, 200)
        self.assertEqual(info["mode"], "mock")
        self.assertEqual(len(turns), 2 * len(measure_latency.STEPS))
        self.assertEqual(
            [t["step"] for t in turns[:5]], ["greeting", "search", "second_one", "stock", "voice"]
        )
        for record in turns:
            self.assertEqual((record["http_status"], record["status"]), (200, "complete"))
            wall = record["wall_ms_from_request_start"]
            self.assertTrue(wall["first_event"] <= wall["turn_end"] <= wall["result"])
            self.assertGreater(record["reply_audio_bytes"], 44)
            self.assertEqual(record["audio_fetch"], "with_result")
            self.assertIsNotNone(record["gateway"]["gateway_total_ms"])
            self.assertIsNotNone(record["gateway"]["tts_ms"])
        by_step = {t["step"]: t for t in turns if t["trial"] == 1}
        self.assertEqual(by_step["greeting"]["tools"], [])
        self.assertIn("search_products", by_step["search"]["tools"])
        self.assertEqual(by_step["stock"]["tools"][-1], "check_availability")
        self.assertEqual(by_step["voice"]["input_source"], "audio")
        self.assertIsNotNone(by_step["voice"]["gateway"]["asr_ms"])
        self.assertNotIn("asr_ms", by_step["search"]["gateway"])
        summary = measure_latency.summarize(turns)
        self.assertEqual(summary["search"]["trials"], 2)
        self.assertIsNotNone(summary["search"]["wall_audio_ms"])
        text = json.dumps(turns)
        for secret in (*KEYS, OPERATOR, "Fixture answer", "rain jacket"):
            self.assertNotIn(secret, text)

    async def test_audio_after_the_result_on_request(self):
        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            _, turns, _ = await self.run_tool(trials=1, audio_fetch="after_result")
        self.assertTrue(all(t["audio_fetch"] == "after_result" for t in turns))
        self.assertTrue(
            all(
                t["wall_ms_from_request_start"]["result"]
                <= t["wall_ms_from_request_start"]["audio"]
                for t in turns
            )
        )

    async def test_inline_measurement_matches_the_new_browser_transport(self):
        with patch.dict(os.environ, {"DEMO_MODE": "mock"}):
            _, turns, _ = await self.run_tool(trials=1, audio_fetch="inline")
        self.assertTrue(all(t["audio_fetch"] == "inline" for t in turns))
        self.assertTrue(all(t["status"] == "complete" for t in turns))
        self.assertTrue(all(t["reply_audio_bytes"] > 44 for t in turns))

    def test_keys_only_travel_to_https_or_this_machine(self):
        for good in ("http://127.0.0.1:8767", "http://localhost:8765/", "https://demo.example"):
            measure_latency.check_origin(good)
        for bad in ("http://10.0.0.5:8765", "http://127.0.0.1:8767/app", "ftp://x"):
            with self.assertRaises(ValueError):
                measure_latency.check_origin(bad)


class LatencySummaryTests(unittest.TestCase):
    def test_fast_failures_cannot_improve_reported_latency(self):
        complete = {
            "step": "voice",
            "http_status": 200,
            "status": "complete",
            "wall_ms_from_request_start": {"audio": 5000},
            "gateway": {"gateway_total_ms": 4500, "model_ms": 2000},
            "reply_audio_bytes": 32000,
            "reply_words": 20,
        }
        failed = complete | {"status": "failed", "wall_ms_from_request_start": {"audio": 1}}
        rejected = {"step": "voice", "http_status": 401, "status": "failed"}
        result = measure_latency.summarize([complete, failed, rejected])["voice"]
        self.assertEqual((result["trials"], result["complete"], result["failed"]), (3, 1, 2))
        self.assertEqual(result["wall_audio_ms"], 5000)

    def test_all_failed_run_reports_no_success_latency(self):
        result = measure_latency.summarize(
            [{"step": "voice", "http_status": 503, "status": "failed"}]
        )["voice"]
        self.assertEqual((result["complete"], result["failed"]), (0, 1))
        self.assertIsNone(result["wall_audio_ms"])
        self.assertIsNone(result["gateway_total_ms"])
