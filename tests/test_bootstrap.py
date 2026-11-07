"""Bootstrap must install a kernel and headers while refusing foreign hosts."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_bootstrap", ROOT / "deploy/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("etc/apt/sources.list.d", "etc/modprobe.d", "var/lib", "proc"):
            (self.root / name).mkdir(parents=True)
        (self.root / "etc/retail-voice-owner").write_text("a" * 32)
        (self.root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        (self.root / "proc/modules").write_text("")
        contexts = (
            patch.object(
                bootstrap, "Path", side_effect=lambda value: self.root / value.lstrip("/")
            ),
            patch.object(bootstrap.os, "getuid", return_value=0),
            patch.object(bootstrap, "download"),
            patch.object(bootstrap.subprocess, "check_output", return_value="synthetic packages"),
            patch.object(
                sys, "argv", ["bootstrap.py", "--owner", "a" * 32, "--admin-cidr", "192.0.2.1/32"]
            ),
        )
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def test_kernel_upgrade_includes_new_dependencies_and_matching_headers(self):
        with patch.object(bootstrap, "run") as run:
            bootstrap.main()
        commands = [call.args for call in run.call_args_list]
        upgrades = [command for command in commands if command[:2] == ("apt-get", "upgrade")]
        self.assertEqual(len(upgrades), 1)
        self.assertIn("--with-new-pkgs", upgrades[0])
        installs = [command for command in commands if command[:2] == ("apt-get", "install")]
        self.assertTrue(
            any(
                "linux-generic-hwe-24.04" in command
                and "linux-headers-generic-hwe-24.04" in command
                for command in installs
            )
        )

    def test_unused_server_modules_are_disabled_and_included_in_boot_image(self):
        with patch.object(bootstrap, "run") as run:
            bootstrap.main()
        policy = (self.root / "etc/modprobe.d/retail-host-security.conf").read_text()
        for module in ("ksmbd", "nvmet", "tipc"):
            self.assertIn(f"blacklist {module}\n", policy)
        run.assert_any_call("update-initramfs", "-u", "-k", "all")

    def test_in_use_module_is_not_silently_disabled(self):
        (self.root / "proc/modules").write_text("nvmet 81920 1 - Live 0x0\n")
        with patch.object(bootstrap, "run"), self.assertRaisesRegex(ValueError, "module is loaded"):
            bootstrap.main()
        self.assertFalse((self.root / "etc/modprobe.d/retail-host-security.conf").exists())

    def test_cleanup_keeps_running_kernel_and_two_newest_images(self):
        names = [
            f"linux-{kind}-{version}{suffix}"
            for version in ("6.8.0-134", "6.8.0-149", "7.0.0-42")
            for kind, suffix in (("image", "-generic"), ("modules", "-generic"), ("headers", ""))
        ]
        inventory = "\n".join(name + "\tinstalled" for name in names)
        removed = bootstrap.obsolete_kernels(inventory, "7.0.0-42-generic")
        self.assertEqual(removed, sorted(name for name in names if "134" in name))
        self.assertEqual(bootstrap.obsolete_kernels(inventory, "6.8.0-134-generic"), [])
        with self.assertRaisesRegex(ValueError, "refusing cleanup"):
            bootstrap.obsolete_kernels(inventory, "unknown-kernel")

    def test_cleanup_refuses_unexpected_dependency_removal(self):
        with (
            patch.object(bootstrap, "obsolete_kernels", return_value=["linux-image-old"]),
            patch.object(
                bootstrap.subprocess,
                "check_output",
                side_effect=["inventory", "Remv unrelated [1.0]\n"],
            ),
            patch.object(bootstrap, "run") as run,
            self.assertRaisesRegex(ValueError, "other packages"),
        ):
            bootstrap.finish_after_reboot()
        run.assert_not_called()

    def test_foreign_host_is_rejected_before_any_command(self):
        (self.root / "etc/retail-voice-owner").write_text("b" * 32)
        with patch.object(bootstrap, "run") as run:
            with self.assertRaisesRegex(ValueError, "does not belong"):
                bootstrap.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
