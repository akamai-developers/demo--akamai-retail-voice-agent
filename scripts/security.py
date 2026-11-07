"""Fail on known dependency findings. Keep complete scan reports in private local storage."""

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from demo import LOCAL, ROOT, private_write

LOCKS = (
    "deploy/installer-requirements.lock.txt",
    "requirements.lock.txt",
    "deploy/speech-requirements.lock.txt",
    "deploy/tryon-requirements.lock.txt",
    "deploy/vlm-requirements.lock.txt",
)


def require_release_clearance():
    path = ROOT / "deploy/release-status.json"
    if not path.is_file():
        raise ValueError("Release status is missing; deployment is blocked")
    status = json.loads(path.read_text())
    if status.get("ready") is not True or status.get("blockers"):
        raise ValueError(
            "Release is blocked. Read docs/validation.md and deploy/release-status.json."
        )


def pins(path):
    values = []
    for line in path.read_text().splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;\\]+)(?:\s*\\)?", line.strip())
        if not match:
            raise ValueError(f"Unexpected unlocked requirement in {path.name}")
        values.append("==".join(match.groups()))
    if not values:
        raise ValueError(f"Empty dependency lock: {path.name}")
    return values


def require_tool(name):
    candidates = [LOCAL / "tools" / name, ROOT / ".security-venv/bin" / name]
    executable = next((str(p) for p in candidates if p.is_file()), None) or shutil.which(name)
    if not executable:
        raise ValueError(f"Install {name} first. See SECURITY.md.")
    return executable


def dependency_audit():
    audit = require_tool("pip-audit")
    directory = LOCAL / "security"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        for index, name in enumerate(LOCKS):
            source = ROOT / name
            requirements = Path(temporary) / f"{index}.txt"
            expected = {
                (re.sub(r"[-_.]+", "-", package.lower()), version)
                for package, version in (pin.split("==", 1) for pin in pins(source))
            }
            requirements.write_text("\n".join(pins(source)) + "\n")
            output = Path(temporary) / f"{index}.json"
            result = subprocess.run(
                [
                    audit,
                    "--disable-pip",
                    "--no-deps",
                    "-r",
                    str(requirements),
                    "--format",
                    "json",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode not in (0, 1) or not output.is_file():
                raise ValueError(f"Dependency scan failed for {name}; no clearance issued")
            report = json.loads(output.read_text())
            dependencies = report.get("dependencies", [])
            covered = {
                (re.sub(r"[-_.]+", "-", package.get("name", "").lower()), package.get("version"))
                for package in dependencies
            }
            failed = (
                result.returncode != 0
                or covered != expected
                or any(p.get("vulns") or p.get("skip_reason") for p in dependencies)
            )
            private_write(directory / f"dependencies-{index}.json", output.read_text())
            results.append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "packages": len(dependencies),
                    "passed": not failed,
                }
            )
            print(f"{name}: {'FAIL' if failed else 'PASS'} ({len(dependencies)} packages)")
    receipt = {
        "created_utc": datetime.now(UTC).isoformat(),
        "locks": results,
        "scope": "All locked PyPI packages. Source patches and native libraries require separate review.",
    }
    private_write(directory / "dependencies.json", json.dumps(receipt, indent=2) + "\n")
    if not all(r["passed"] for r in results):
        raise ValueError("Known findings or unscanned dependencies block release")


def container_audit(image, destination, required_types):
    trivy = require_tool("trivy")
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "scan.json"
        result = subprocess.run(
            [
                trivy,
                "image",
                "--scanners",
                "vuln",
                "--ignorefile",
                "/dev/null",
                "--severity",
                "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
                "--exit-code",
                "1",
                "--format",
                "json",
                "--timeout",
                "30m",
                "--output",
                str(output),
                image,
            ],
            check=False,
        )
        if not output.is_file():
            raise ValueError("Container scan did not produce a report")
        report = json.loads(output.read_text())
        private_write(destination, output.read_text())
        coverage = {entry.get("Type") for entry in report.get("Results", [])}
        if (
            result.returncode
            or not required_types
            or not set(required_types).issubset(coverage)
            or any(r.get("Vulnerabilities") for r in report["Results"])
        ):
            raise ValueError("Known container findings or scan failure block release")


def host_assessment(report, running_kernel):
    """Describe package matches without treating scan matches as proven exploits."""
    findings = [v for row in report.get("Results", []) for v in row.get("Vulnerabilities", [])]
    active_names = {
        "linux-image-" + running_kernel,
        "linux-image-unsigned-" + running_kernel,
        "linux-modules-" + running_kernel,
        "linux-modules-extra-" + running_kernel,
    }

    def summarize(values):
        unique = {v["VulnerabilityID"]: v for v in values}
        return {
            "package_matches": len(values),
            "unique_advisories": len(unique),
            "unique_by_severity": dict(
                Counter(v.get("Severity", "UNKNOWN") for v in unique.values())
            ),
            "matches_with_fixed_version": sum(bool(v.get("FixedVersion")) for v in values),
        }

    active = [v for v in findings if v.get("PkgName") in active_names]
    return {
        "created_utc": datetime.now(UTC).isoformat(),
        "running_kernel": running_kernel,
        "all_installed": summarize(findings),
        "active_kernel_packages": summarize(active),
        "other_installed_packages": summarize(
            [v for v in findings if v.get("PkgName") not in active_names]
        ),
        "active_kernel_high_critical": sorted(
            {v["VulnerabilityID"] for v in active if v.get("Severity") in {"HIGH", "CRITICAL"}}
        ),
        "interpretation": "Package matches require applicability review. Other installed packages include fallback kernels, headers, tools, and user-space libraries. No findings are suppressed.",
    }


def validate_host_report(report, returncode, running_kernel, *, accept_host_advisories=False):
    os_info = report.get("Metadata", {}).get("OS", {})
    rows = report.get("Results", [])
    packages = {
        p.get("Name") for r in rows if r.get("Type") == "ubuntu" for p in r.get("Packages", [])
    }
    kernel_covered = "linux-modules-" + running_kernel in packages and bool(
        {"linux-image-" + running_kernel, "linux-image-unsigned-" + running_kernel} & packages
    )
    if (
        returncode not in (0, 1)
        or os_info.get("Family") != "ubuntu"
        or os_info.get("Name") != "24.04"
        or not kernel_covered
    ):
        raise ValueError("Host scan failed or does not cover the running Ubuntu kernel")
    findings = [v for row in rows for v in row.get("Vulnerabilities", [])]
    if returncode and not findings:
        raise ValueError("Host scanner failed without reporting findings")
    if any(v.get("FixedVersion") for v in findings):
        raise ValueError(
            "Host findings with available fixes block installation; apply updates first"
        )
    if findings and not accept_host_advisories:
        raise ValueError(
            "Host findings block installation; review .local/security/host-assessment.json and the complete host.json report"
        )
    if findings:
        print(
            "Restricted demo: unfixed host advisories accepted explicitly. Full findings are retained; this is not security clearance."
        )


def host_audit(*, accept_host_advisories=False):
    trivy = require_tool("trivy")
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        inventory = directory / "inventory"
        for name in (
            "etc/os-release",
            "usr/lib/os-release",
            "etc/lsb-release",
            "etc/debian_version",
            "var/lib/dpkg/status",
        ):
            source = Path("/") / name
            if source.is_file():
                target = inventory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        output = directory / "host.json"
        result = subprocess.run(
            [
                trivy,
                "rootfs",
                "--scanners",
                "vuln",
                "--pkg-types",
                "os",
                "--list-all-pkgs",
                "--ignorefile",
                "/dev/null",
                "--severity",
                "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
                "--exit-code",
                "1",
                "--format",
                "json",
                "--output",
                str(output),
                str(inventory),
            ],
            check=False,
        )
        if not output.is_file():
            raise ValueError("Host scan did not produce a report")
        report = json.loads(output.read_text())
        private_write(LOCAL / "security/host.json", output.read_text())
        running_kernel = platform.release()
        assessment = host_assessment(report, running_kernel)
        assessment["report_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
        assessment["scanner_exit"] = result.returncode
        assessment["unfixed_advisories_accepted_for_demo"] = accept_host_advisories
        private_write(
            LOCAL / "security/host-assessment.json", json.dumps(assessment, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    key: assessment[key]
                    for key in ("running_kernel", "all_installed", "active_kernel_packages")
                },
                indent=2,
            )
        )
        validate_host_report(
            report, result.returncode, running_kernel, accept_host_advisories=accept_host_advisories
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["dependencies", "host", "release"])
    parser.add_argument(
        "--accept-host-advisories",
        action="store_true",
        help="Accept documented unfixed host findings for a restricted demo",
    )
    args = parser.parse_args()
    if args.command == "release":
        require_release_clearance()
    elif args.command == "host":
        host_audit(accept_host_advisories=args.accept_host_advisories)
    else:
        dependency_audit()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
