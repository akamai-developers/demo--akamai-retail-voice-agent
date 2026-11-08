"""Cloud probes must reject mock inference and close their own test sessions."""

import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("cloud_probe_checks", SCRIPTS / "cloud_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class CloudProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.values = {
            "TENANT_A_KEY": "synthetic-a",
            "TENANT_B_KEY": "synthetic-b",
            "TRYON_MODE": "live",
        }
        self.mode = "nvidia"
        self.ready = True
        self.shared_catalog = False
        self.allow_unauthenticated = False
        self.tryon_mode = "live"
        self.deleted = []

    def response(self, request):
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(
                200,
                json={
                    "mode": self.mode,
                    "site": "test-site",
                    "fixture_enabled": self.mode == "mock",
                    "tryon": {"mode": self.tryon_mode},
                },
            )
        if path == "/health":
            return httpx.Response(200, json={"ready": self.ready})
        if path == "/v1/catalog":
            token = request.headers.get("authorization", "")
            if token not in ("Bearer synthetic-a", "Bearer synthetic-b"):
                return httpx.Response(200 if self.allow_unauthenticated else 401, json={})
            product = "a" if self.shared_catalog or token.endswith("-a") else "b"
            return httpx.Response(200, json={"products": [{"product_id": product}]})
        if path == "/v1/sessions" and request.method == "POST":
            return httpx.Response(
                201, json={"session_id": "test-session", "session_token": "synthetic-session-token"}
            )
        if path == "/v1/sessions/test-session" and request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(204)
        if path == "/preview":
            return httpx.Response(200, content=b"synthetic-image")
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(
            base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(self.response)
        )

    async def test_readiness_checks_authentication_tenant_scope_and_preview_worker(self):
        async with self.client() as client:
            report = await probe.check(client, self.values)
        self.assertEqual(report["authentication"], "passed")
        self.assertEqual(report["catalog_isolation"], "passed")

    async def test_mock_worker_failure_and_authentication_leaks_cannot_pass(self):
        for field, value in (
            ("mode", "mock"),
            ("ready", False),
            ("shared_catalog", True),
            ("allow_unauthenticated", True),
        ):
            self.mode, self.ready, self.shared_catalog, self.allow_unauthenticated = (
                "nvidia",
                True,
                False,
                False,
            )
            setattr(self, field, value)
            async with self.client() as client:
                with self.assertRaises(ValueError):
                    await probe.check(client, self.values)

    async def test_live_preview_cart_and_sold_out_sequence(self):
        cart = [{"variant_id": "a-drizzle-anorak-navy-m", "quantity": 1}]
        responses = [
            {
                "status": "complete",
                "selected_variant_id": cart[0]["variant_id"],
                "preview": {"kind": "live", "image_path": "/preview"},
            },
            {"status": "complete", "cart": cart},
            {"status": "complete", "cart": cart, "answer": "Yellow medium is out of stock."},
        ]
        calls = AsyncMock(side_effect=[({}, body, b"synthetic-audio") for body in responses])
        report = {}
        with patch("measure_latency.turn", calls):
            async with self.client() as client:
                await probe.shopping(client, self.values, report)
        self.assertEqual(len(report["shopping"]), 3)
        self.assertEqual(report["preview_sha256"], hashlib.sha256(b"synthetic-image").hexdigest())
        self.assertEqual(len(self.deleted), 1)

    async def test_prepared_preview_is_rejected_and_session_closed(self):
        body = {
            "status": "complete",
            "preview": {"kind": "prepared"},
            "selected_variant_id": "a-drizzle-anorak-navy-m",
        }
        with patch("measure_latency.turn", AsyncMock(return_value=({}, body, b"audio"))):
            async with self.client() as client:
                with self.assertRaisesRegex(ValueError, "live navy medium"):
                    await probe.shopping(client, self.values, {})
        self.assertEqual(len(self.deleted), 1)

    async def test_sold_out_turn_cannot_change_the_cart(self):
        cart = [{"variant_id": "a-drizzle-anorak-navy-m", "quantity": 1}]
        responses = [
            {
                "status": "complete",
                "selected_variant_id": cart[0]["variant_id"],
                "preview": {"kind": "live", "image_path": "/preview"},
            },
            {"status": "complete", "cart": cart},
            {
                "status": "complete",
                "cart": [{"variant_id": "a-downpour-packable-shell-yellow-m", "quantity": 1}],
                "answer": "Yellow medium is out of stock.",
            },
        ]
        with patch(
            "measure_latency.turn",
            AsyncMock(side_effect=[({}, body, b"audio") for body in responses]),
        ):
            async with self.client() as client:
                with self.assertRaisesRegex(ValueError, "Sold-out"):
                    await probe.shopping(client, self.values, {})
        self.assertEqual(len(self.deleted), 1)

    async def test_disabled_tryon_requires_disabled_gateway_and_skips_worker(self):
        values = self.values | {"TRYON_MODE": "disabled"}
        self.tryon_mode = "disabled"
        self.ready = False
        async with self.client() as client:
            report = await probe.check(client, values)
        self.assertEqual(report["tryon"], "disabled")
        self.tryon_mode = "live"
        async with self.client() as client:
            with self.assertRaisesRegex(ValueError, "Expected disabled"):
                await probe.check(client, values)

    async def test_disabled_tryon_smoke_still_checks_cart_and_sold_out(self):
        cart = [{"variant_id": "a-drizzle-anorak-navy-m", "quantity": 1}]
        responses = [
            {"status": "complete", "selected_variant_id": cart[0]["variant_id"]},
            {"status": "complete", "cart": cart},
            {"status": "complete", "cart": cart, "answer": "Yellow medium is out of stock."},
        ]
        calls = AsyncMock(side_effect=[({}, body, b"synthetic-audio") for body in responses])
        report = {}
        with patch("measure_latency.turn", calls):
            async with self.client() as client:
                await probe.shopping(client, self.values | {"TRYON_MODE": "disabled"}, report)
        self.assertEqual(len(report["shopping"]), 3)
        self.assertEqual(report["preview"], "skipped: TRYON_ENABLED=false")
        self.assertNotIn("preview_sha256", report)
        self.assertNotIn("try it on", calls.call_args_list[0].kwargs["text"])
        self.assertIn("Add the Downpour", calls.call_args_list[2].kwargs["text"])
        self.assertEqual(len(self.deleted), 1)


if __name__ == "__main__":
    unittest.main()
