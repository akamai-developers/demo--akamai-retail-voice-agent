"""Provisioning tests use a fake cloud and cannot create billable resources."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("site_deploy", SCRIPTS / "site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
security = importlib.import_module("security")
speech_compat = importlib.import_module("speech_compat")


class FakeCloud:
    def __init__(self, rows=(), fail_create=False):
        self.resources = list(rows)
        self.writes = []
        self.fail_create = fail_create

    def rows(self, path):
        return self.resources

    def request(self, path, payload=None):
        self.writes.append((path, payload))
        if self.fail_create:
            raise OSError("Synthetic connection loss after request")
        row = payload | {"id": 123}
        self.resources.append(row)
        return row


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.state = {"owner": "a" * 32}
        self.saved = []

    def save(self):
        self.saved.append(json.loads(json.dumps(self.state)))

    def ensure(self, cloud):
        return site.ensure_resource(
            cloud,
            "/linode/instances",
            "retail-sample",
            {"label": "retail-sample", "tags": [self.state["owner"]]},
            self.state,
            "instance",
            self.save,
        )

    def test_create_then_resume_does_not_create_another_host(self):
        cloud = FakeCloud()
        self.assertEqual(self.ensure(cloud)["id"], 123)
        self.assertEqual(self.ensure(cloud)["id"], 123)
        self.assertEqual(len(cloud.writes), 1)
        self.assertTrue(self.saved[0]["instance_pending"])

    def test_ambiguous_creation_is_not_retried(self):
        cloud = FakeCloud(fail_create=True)
        with self.assertRaises(OSError):
            self.ensure(cloud)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.ensure(cloud)
        self.assertEqual(len(cloud.writes), 1)

    def test_existing_foreign_resource_is_never_adopted(self):
        cloud = FakeCloud([{"id": 1, "label": "retail-sample", "tags": ["someone-else"]}])
        with self.assertRaisesRegex(ValueError, "another deployment"):
            self.ensure(cloud)
        self.assertEqual(cloud.writes, [])

    def test_owned_creation_recovers_a_lost_response(self):
        self.state["instance_pending"] = True
        cloud = FakeCloud([{"id": 123, "label": "retail-sample", "tags": [self.state["owner"]]}])
        self.assertEqual(self.ensure(cloud)["id"], 123)
        self.assertEqual(cloud.writes, [])

    def test_replacement_instance_is_rejected(self):
        self.state["instance"] = 999
        cloud = FakeCloud([{"id": 123, "label": "retail-sample", "tags": [self.state["owner"]]}])
        with self.assertRaises(ValueError):
            self.ensure(cloud)
        self.assertEqual(cloud.writes, [])

    def test_existing_site_inputs_cannot_change(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "state.json").write_text(
                json.dumps({"owner": "a" * 32, "config": {"site": "old-site", "region": "nl-ams"}})
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                site.apply(FakeCloud(), {"site": "old-site", "region": "us-sea"}, None, folder)

    def test_firewall_cannot_silently_broaden(self):
        config = {"admin_cidrs": ["192.0.2.1/32", "198.51.100.2/32"]}
        wanted = site.firewall_rules(config)
        site.verify_firewall(wanted, config)
        for key, value in (("inbound_policy", "ACCEPT"), ("outbound", [{}])):
            with self.assertRaises(ValueError):
                site.verify_firewall(wanted | {key: value}, config)
        wanted["inbound"][0]["addresses"]["ipv4"] = ["0.0.0.0/0"]
        with self.assertRaisesRegex(ValueError, "audience"):
            site.verify_firewall(wanted, config)

    def test_candidate_verification_does_not_clear_publication(self):
        with patch.object(
            site, "require_release_clearance", side_effect=ValueError("blocked")
        ) as gate:
            with self.assertRaisesRegex(ValueError, "blocked"):
                site.deployment_clearance(False)
            site.deployment_clearance(True)
            self.assertEqual(gate.call_count, 1)
            with self.assertRaisesRegex(ValueError, "blocked"):
                site.deployment_clearance(False)

    def test_no_capacity_stops_before_new_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No capacity"):
                site.apply(
                    FakeCloud(), {"site": "new-site"}, None, Path(directory), available=False
                )

    def test_cloud_api_has_no_update_or_delete_path(self):
        cloud = object.__new__(site.Cloud)
        cloud.token = "synthetic-test-value"
        with self.assertRaisesRegex(ValueError, "Only creation"):
            cloud.request("/linode/instances/123", {"label": "modified"})

    def test_cloud_lists_cover_availability_and_paginated_resources(self):
        cloud = object.__new__(site.Cloud)
        rows = [{"plan": "gpu-plan", "available": True}]
        with patch.object(cloud, "request", return_value=rows):
            self.assertEqual(cloud.rows("/regions/nl-ams/availability"), rows)
        with patch.object(
            cloud,
            "request",
            side_effect=[{"data": [{"id": 1}], "pages": 2}, {"data": [{"id": 2}], "pages": 2}],
        ) as request:
            self.assertEqual(cloud.rows("/linode/instances"), [{"id": 1}, {"id": 2}])
            self.assertEqual(request.call_args.args[0], "/linode/instances?page_size=100&page=2")

    def test_cloud_credentials_cannot_follow_redirects(self):
        with self.assertRaisesRegex(ValueError, "redirects are refused"):
            site.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_vendor_changes_require_exact_reviewed_source(self):
        with self.assertRaisesRegex(ValueError, "source changed"):
            speech_compat.transform(b"unexpected vendor file", "a" * 64, "b" * 64, ())

    def test_all_runtime_locks_are_complete_pins(self):
        root = SCRIPTS.parent
        for lock in security.LOCKS:
            self.assertGreaterEqual(
                len(security.pins(root / lock)), 1 if "installer-" in lock else 51
            )


if __name__ == "__main__":
    unittest.main()
