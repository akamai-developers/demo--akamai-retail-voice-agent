"""Installer checks use disposable clones and never download models or start services."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(
            ROOT / "scripts", self.root / "scripts", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copytree(ROOT / "deploy", self.root / "deploy")

    def command(self, script, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(self.root / "scripts" / script), *args],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_keys_are_private_unique_and_preserved(self):
        result = self.command("demo.py", "init")
        env = self.root / ".local/gateway.env"
        first = env.read_text()
        keys = [json.loads(line.split("=", 1)[1]) for line in first.splitlines() if "_KEY=" in line]
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(len(key) >= 32 for key in keys))
        self.assertTrue(all(key not in result.stdout + result.stderr for key in keys))
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(env.parent.stat().st_mode), 0o700)
        self.command("demo.py", "init")
        self.assertEqual(first, env.read_text())
        self.command("demo.py", "rotate-keys")
        self.assertNotEqual(first, env.read_text())

    def test_symlink_configuration_rejected(self):
        private = self.root / "private"
        private.mkdir()
        (self.root / ".local").symlink_to(private, target_is_directory=True)
        self.command("demo.py", "init", success=False)
        self.assertEqual(list(private.iterdir()), [])

    def test_gpu_config_and_units_use_clone_paths(self):
        self.command("gpu.py", "configure", "--region", "us-sea")
        config = json.loads((self.root / ".local/gpu.json").read_text())
        self.assertEqual(config["region"], "us-sea")
        self.assertEqual(config["context_tokens"], 16384)
        self.assertTrue(Path(config["runtime_dir"]).is_absolute())
        if os.getuid() == 0:
            self.command("gpu.py", "services", success=False)
            return
        (self.root / ".local/caddy-image.json").write_text(
            json.dumps({"image": "sha256:" + "a" * 64, "vulnerability_scan": "passed"})
        )
        self.command(
            "gpu.py", "services", "--domain", "demo.example.com", "--email", "admin@example.com"
        )
        units = self.root / ".local/systemd"
        self.assertEqual(len(list(units.glob("*.service"))), 4)
        gateway = (units / "retail-gateway.service").read_text()
        self.assertIn(str(self.root), gateway)
        self.assertIn("--host 127.0.0.1 --port 8765", gateway)
        self.assertNotIn("TENANT_A_KEY", gateway)
        self.assertIn("NoNewPrivileges=yes", gateway)
        self.assertIn("InaccessiblePaths=-/run/docker.sock", gateway)
        self.assertIn("demo.example.com", (self.root / ".local/Caddyfile").read_text())

    def test_existing_environment_symlink_is_rejected(self):
        folder = self.root / ".local"
        folder.mkdir()
        target = self.root / "outside.env"
        target.write_text("unrelated file")
        (folder / "gateway.env").symlink_to(target)
        self.command("demo.py", "init", success=False)
        self.assertEqual(target.read_text(), "unrelated file")

    def test_invalid_region_does_not_write_gpu_configuration(self):
        self.command("gpu.py", "configure", "--region", "bad\nvalue", success=False)
        self.assertFalse((self.root / ".local/gpu.json").exists())

    def test_explicit_runtime_settings_reach_private_host_config(self):
        self.command(
            "gpu.py",
            "configure",
            "--region",
            "nl-ams",
            "--model-profile",
            "retail-full",
            "--context-tokens",
            "8192",
            "--gpu-memory-utilization",
            "0.65",
        )
        config = json.loads((self.root / ".local/gpu.json").read_text())
        self.assertEqual(config["context_tokens"], 8192)
        self.assertEqual(config["gpu_memory_utilization"], 0.65)
        values = dict(
            line.split("=", 1)
            for line in (self.root / ".local/gateway.env").read_text().splitlines()
        )
        self.assertEqual(json.loads(values["TRYON_MODE"]), "disabled")
        if os.getuid() != 0:
            self.command("gpu.py", "services")
            self.assertFalse((self.root / ".local/systemd/retail-tryon.service").exists())

    def test_invalid_runtime_settings_do_not_overwrite_host_config(self):
        self.command("gpu.py", "configure", "--region", "nl-ams")
        path = self.root / ".local/gpu.json"
        before = path.read_bytes()
        for flag, value in (
            ("--model-profile", "compact"),
            ("--context-tokens", "50000"),
            ("--gpu-memory-utilization", "nan"),
        ):
            self.command("gpu.py", "configure", "--region", "nl-ams", flag, value, success=False)
            self.assertEqual(path.read_bytes(), before)

    def test_model_download_requires_explicit_acceptance(self):
        result = self.command("gpu.py", "install", success=False)
        self.assertIn("accept-model-licenses", result.stderr)
        self.assertFalse((self.root / ".speech-venv").exists())

    def test_demo_person_configuration_uses_only_the_bundled_fictional_image(self):
        target = self.root / "src/retail_voice/data/demo"
        shutil.copytree(ROOT / "src/retail_voice/data/demo", target)
        self.command("gpu.py", "configure", "--region", "nl-ams", "--demo-person")
        values = {
            key: json.loads(value)
            for key, value in (
                line.split("=", 1)
                for line in (self.root / ".local/gateway.env").read_text().splitlines()
            )
        }
        self.assertEqual(values["TRYON_MODE"], "live")
        self.assertEqual(values["TRYON_REFERENCE_IMAGE"], str(target / "fictional-person.png"))
        self.assertEqual(values["TRYON_TENANT_ID"], "retailer-a")
        self.command(
            "gpu.py",
            "configure",
            "--region",
            "nl-ams",
            "--demo-person",
            "--reference",
            "/private/person.png",
            success=False,
        )
        (target / "fictional-person.png").write_bytes(b"unreviewed replacement")
        result = self.command(
            "gpu.py", "configure", "--region", "nl-ams", "--demo-person", success=False
        )
        self.assertIn("does not match its provenance", result.stderr)

    def test_reviewed_readme_recording_exports_but_changed_or_other_gifs_fail(self):
        target = self.root / "docs/media"
        shutil.copytree(ROOT / "docs/media", target)
        archive_path = self.root / "review.zip"
        self.command("release.py", "--output", str(archive_path))
        import zipfile

        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("retail-voice-demo/docs/media/retail-demo.gif", archive.namelist())
        gif = target / "retail-demo.gif"
        original = gif.read_bytes()
        gif.write_bytes(original + b"unreviewed change")
        result = self.command("public_check.py", success=False)
        self.assertIn("does not match its provenance", result.stderr)
        gif.write_bytes(original)
        (target / "unreviewed.gif").write_bytes(original)
        result = self.command("public_check.py", success=False)
        self.assertIn("Unexpected recording", result.stderr)

    def test_export_includes_synthetic_person_and_rejects_other_person_photos(self):
        target = self.root / "src/retail_voice/data/demo"
        shutil.copytree(ROOT / "src/retail_voice/data/demo", target)
        self.command("demo.py", "init")
        (self.root / ".local/private-person.png").write_bytes(b"private fixture")
        archive_path = self.root / "review.zip"
        self.command("release.py", "--output", str(archive_path))
        import zipfile

        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn(
                "retail-voice-demo/src/retail_voice/data/demo/fictional-person.png",
                archive.namelist(),
            )
            self.assertFalse(any("private-person" in name for name in archive.namelist()))
        (target / "another-person.png").write_bytes(b"unapproved fixture")
        result = self.command("public_check.py", success=False)
        self.assertIn("Unexpected person or image asset", result.stderr)

    def test_export_excludes_secrets_and_history(self):
        self.command("demo.py", "init")
        (self.root / ".git").mkdir()
        (self.root / ".git/private-history").write_text("private")
        output = self.root / "review.zip"
        self.command("release.py", "--output", str(output))
        import zipfile

        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertFalse(any("/.local/" in name or "/.git/" in name for name in names))
            self.assertIn("retail-voice-demo/scripts/demo.py", names)
        self.command(
            "release.py", "--output", str(self.root / "publish.zip"), "--publish", success=False
        )

    def test_tracked_private_files_fail_the_public_check(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / "private-notes.txt").write_text("Synthetic private content")
        subprocess.run(["git", "add", "private-notes.txt"], cwd=self.root, check=True)
        result = self.command("public_check.py", success=False)
        self.assertIn("outside the public allowlist", result.stderr)
        self.assertNotIn("Synthetic private content", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
