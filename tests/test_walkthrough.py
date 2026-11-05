"""The recorded walkthrough route: served only from a valid WALKTHROUGH_DIR, without a key."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from fastapi.testclient import TestClient

from retail_voice.api import create_app
from retail_voice.models.mock import MockBackend
from retail_voice.walkthrough import WalkthroughError, validate

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"tenant-a-test-key-0001": "retailer-a", "tenant-b-test-key-0002": "retailer-b"}
OPERATOR = "operator-test-key-0003"
EXAMPLE = json.loads((ROOT / "contracts/walkthrough.example.json").read_text())
SCHEMA = json.loads((ROOT / "contracts/walkthrough.schema.json").read_text())
VTT = "WEBVTT\n\n00:00.000 --> 00:03.000\nPress Start conversation once.\n"
VIDEO = bytes(range(256)) * 16  # 4 KB stand-in; the route never decodes it


def directory(manifest=EXAMPLE, vtt=VTT, video=VIDEO):
    folder = tempfile.TemporaryDirectory()
    root = Path(folder.name)
    (root / "walkthrough.json").write_text(json.dumps(manifest))
    (root / "walkthrough.vtt").write_text(vtt)
    (root / "walkthrough.mp4").write_bytes(video)
    return folder


class WalkthroughRouteTests(unittest.TestCase):
    def app(self, folder=None):
        env = {"DEMO_MODE": "mock"} | ({"WALKTHROUGH_DIR": folder} if folder else {})
        with patch.dict(os.environ, env):
            if not folder:
                os.environ.pop("WALKTHROUGH_DIR", None)
            return create_app(KEYS, OPERATOR, backend=MockBackend())

    def test_served_without_a_key_with_types_and_byte_ranges(self):
        folder = directory()
        self.addCleanup(folder.cleanup)
        with TestClient(self.app(folder.name)) as client:
            self.assertTrue(client.get("/v1/info").json()["walkthrough"])
            for name, kind in (
                ("walkthrough.json", "application/json"),
                ("walkthrough.vtt", "text/vtt; charset=utf-8"),
                ("walkthrough.mp4", "video/mp4"),
            ):
                response = client.get(f"/app/walkthrough/{name}")
                self.assertEqual(response.status_code, 200, name)
                self.assertEqual(response.headers["content-type"], kind)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(client.get("/app/walkthrough/walkthrough.json").json(), EXAMPLE)
            head = client.head("/app/walkthrough/walkthrough.mp4")
            self.assertEqual((head.status_code, head.content), (200, b""))
            self.assertEqual(head.headers["content-type"], "video/mp4")
            ranged = client.get("/app/walkthrough/walkthrough.mp4", headers={"Range": "bytes=0-99"})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, VIDEO[:100])
            self.assertEqual(ranged.headers["content-range"], f"bytes 0-99/{len(VIDEO)}")
            for other in ("index.html", "walkthrough.mov", "..%2Fwalkthrough.json", "app.js"):
                self.assertEqual(client.get(f"/app/walkthrough/{other}").status_code, 404, other)
            # The page assets still come from the package.
            self.assertEqual(client.get("/app/app.js").status_code, 200)

    def test_without_the_directory_nothing_is_served(self):
        with TestClient(self.app()) as client:
            self.assertFalse(client.get("/v1/info").json()["walkthrough"])
            for name in ("walkthrough.json", "walkthrough.mp4", "walkthrough.vtt"):
                response = client.get(f"/app/walkthrough/{name}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "walkthrough_unavailable"})

    def test_a_broken_directory_stops_startup(self):
        bad = {
            "chapters out of order": directory(
                EXAMPLE | {"chapters": [{"start": 5, "title": "b"}, {"start": 5, "title": "c"}]}
            ),
            "other captions": directory(EXAMPLE | {"captions": "other.vtt"}),
            "no build": directory({k: v for k, v in EXAMPLE.items() if k != "build"}),
            "extra field": directory(EXAMPLE | {"autoplay": True}),
            "bad date": directory(EXAMPLE | {"recorded": "Nov 5"}),
            "not webvtt": directory(vtt="1\n00:00:00,000 --> 00:00:01,000\nhi\n"),
            "tiny video": directory(video=b"x" * 10),
            "not json": directory(),
        }
        (Path(bad["not json"].name) / "walkthrough.json").write_text("{not json")
        missing = directory()
        (Path(missing.name) / "walkthrough.vtt").unlink()
        bad["missing captions"] = missing
        outside = directory()
        escape = directory()
        (Path(escape.name) / "walkthrough.mp4").unlink()
        (Path(escape.name) / "walkthrough.mp4").symlink_to(Path(outside.name) / "walkthrough.mp4")
        bad["link outside"] = escape
        for folder in (*bad.values(), outside):
            self.addCleanup(folder.cleanup)
        for label, folder in bad.items():
            with self.subTest(label=label), self.assertRaises(WalkthroughError):
                self.app(folder.name)
        with self.assertRaises(WalkthroughError):
            self.app("relative/walkthrough")
        with self.assertRaises(WalkthroughError):
            self.app("/no/such/walkthrough/dir")

    def test_example_matches_the_schema_and_the_gateway_rules(self):
        jsonschema.validate(EXAMPLE, SCHEMA)
        validate(EXAMPLE)
        for broken in (
            EXAMPLE | {"chapters": []},
            EXAMPLE | {"chapters": [{"start": -1, "title": "x"}]},
            EXAMPLE | {"chapters": [{"start": 0, "title": ""}]},
            EXAMPLE | {"chapters": [{"start": 0, "title": "x", "extra": 1}]},
            EXAMPLE | {"build": "x"},
            EXAMPLE | {"schema_version": 2},
            EXAMPLE | {"duration_seconds": "long"},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(broken, SCHEMA)
                with self.assertRaises(WalkthroughError):
                    validate(broken)
