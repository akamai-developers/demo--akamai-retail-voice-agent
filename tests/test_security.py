"""Incomplete or failing advisory checks must never issue release clearance."""

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
security = importlib.import_module("security")


class SecurityGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.local = self.root / ".local"
        for name, value in (("ROOT", self.root), ("LOCAL", self.local)):
            context = patch.object(security, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(security, "require_tool", return_value="synthetic-scanner")
        context.start()
        self.addCleanup(context.stop)

    def scanner(self, report, returncode=0):
        def run(command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(report))
            return subprocess.CompletedProcess(command, returncode)

        return patch.object(security.subprocess, "run", side_effect=run)

    def dependency_scan(self, packages, returncode=0):
        lock = self.root / "requirements.txt"
        lock.write_text("example-one==1.0\nexample-two==2.0\n")
        with (
            patch.object(security, "LOCKS", (lock.name,)),
            self.scanner({"dependencies": packages}, returncode),
        ):
            security.dependency_audit()

    def test_partial_empty_and_skipped_dependency_reports_fail(self):
        one = {"name": "example-one", "version": "1.0", "vulns": []}
        two = {"name": "example-two", "version": "2.0", "vulns": []}
        for packages in ([], [one], [one, two | {"skip_reason": "unavailable"}]):
            with self.subTest(packages=packages), self.assertRaisesRegex(ValueError, "block"):
                self.dependency_scan(packages)

    def test_known_findings_and_scanner_failure_fail(self):
        one = {"name": "example-one", "version": "1.0", "vulns": []}
        two = {"name": "example-two", "version": "2.0", "vulns": []}
        for packages, code in (([one, two | {"vulns": [{"id": "synthetic"}]}], 0), ([one, two], 1)):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "block"):
                self.dependency_scan(packages, code)

    def test_complete_dependency_coverage_is_required(self):
        self.dependency_scan(
            [{"name": "example_one", "version": "1.0"}, {"name": "example-two", "version": "2.0"}]
        )
        receipt = json.loads((self.local / "security/dependencies.json").read_text())
        self.assertTrue(receipt["locks"][0]["passed"])

    def test_model_image_scan_requires_os_and_python_coverage(self):
        for results in ([], [{"Type": "python-pkg"}], [{"Type": "unknown"}]):
            with self.subTest(results=results), self.scanner({"Results": results}):
                with self.assertRaisesRegex(ValueError, "block"):
                    security.container_audit(
                        "synthetic-image", self.local / "image.json", {"wolfi", "python-pkg"}
                    )

    def test_container_findings_and_scanner_errors_fail(self):
        for findings, code in (([{"VulnerabilityID": "synthetic"}], 0), ([], 1)):
            with (
                self.subTest(code=code),
                self.scanner(
                    {"Results": [{"Type": "gobinary", "Vulnerabilities": findings}]}, code
                ),
            ):
                with self.assertRaisesRegex(ValueError, "block"):
                    security.container_audit(
                        "synthetic-image", self.local / "image.json", {"gobinary"}
                    )

    def test_host_scan_requires_running_kernel_package_coverage(self):
        kernel = "7.0.0-42-generic"
        row = {
            "Type": "ubuntu",
            "Packages": [{"Name": "linux-image-" + kernel}, {"Name": "linux-modules-" + kernel}],
        }
        report = {"Metadata": {"OS": {"Family": "ubuntu", "Name": "24.04"}}, "Results": [row]}
        security.validate_host_report(report, 0, kernel)
        for invalid in (report | {"Results": []}, report | {"Results": [row | {"Packages": []}]}):
            with self.assertRaisesRegex(ValueError, "does not cover"):
                security.validate_host_report(invalid, 0, kernel)
        for code in (1, 2):
            with self.assertRaises(ValueError):
                security.validate_host_report(report, code, kernel)
        for severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"):
            finding = {"VulnerabilityID": "synthetic", "Severity": severity}
            with self.assertRaisesRegex(ValueError, "findings block"):
                security.validate_host_report(
                    report | {"Results": [row | {"Vulnerabilities": [finding]}]}, 0, kernel
                )

    def test_demo_acceptance_keeps_fixable_findings_and_scan_failure_blocking(self):
        kernel = "7.0.0-42-generic"
        finding = {"VulnerabilityID": "synthetic-critical", "Severity": "CRITICAL"}
        row = {
            "Type": "ubuntu",
            "Packages": [{"Name": "linux-image-" + kernel}, {"Name": "linux-modules-" + kernel}],
            "Vulnerabilities": [finding],
        }
        report = {"Metadata": {"OS": {"Family": "ubuntu", "Name": "24.04"}}, "Results": [row]}
        security.validate_host_report(report, 1, kernel, accept_host_advisories=True)
        with self.assertRaisesRegex(ValueError, "available fixes"):
            security.validate_host_report(
                report
                | {"Results": [row | {"Vulnerabilities": [finding | {"FixedVersion": "2.0"}]}]},
                1,
                kernel,
                accept_host_advisories=True,
            )
        for invalid, code in (
            (report | {"Results": []}, 1),
            (report, 2),
            (report | {"Results": [row | {"Vulnerabilities": []}]}, 1),
        ):
            with self.assertRaises(ValueError):
                security.validate_host_report(invalid, code, kernel, accept_host_advisories=True)

    def test_host_assessment_keeps_inactive_and_duplicate_matches_visible(self):
        kernel = "7.0.0-42-generic"
        findings = [
            {
                "VulnerabilityID": "synthetic-one",
                "Severity": "HIGH",
                "PkgName": "linux-modules-" + kernel,
            },
            {
                "VulnerabilityID": "synthetic-one",
                "Severity": "HIGH",
                "PkgName": "linux-headers-old",
            },
            {
                "VulnerabilityID": "synthetic-two",
                "Severity": "LOW",
                "PkgName": "example",
                "FixedVersion": "2.0",
            },
        ]
        summary = security.host_assessment({"Results": [{"Vulnerabilities": findings}]}, kernel)
        self.assertEqual(summary["all_installed"]["package_matches"], 3)
        self.assertEqual(summary["all_installed"]["unique_advisories"], 2)
        self.assertEqual(summary["active_kernel_packages"]["package_matches"], 1)
        self.assertEqual(summary["other_installed_packages"]["package_matches"], 2)
        self.assertEqual(summary["all_installed"]["matches_with_fixed_version"], 1)

    def test_missing_or_blocked_release_status_fails(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            security.require_release_clearance()
        (self.root / "deploy").mkdir()
        for status in ({"ready": False}, {"ready": True, "blockers": ["unresolved"]}):
            (self.root / "deploy/release-status.json").write_text(json.dumps(status))
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "blocked"):
                security.require_release_clearance()


if __name__ == "__main__":
    unittest.main()
