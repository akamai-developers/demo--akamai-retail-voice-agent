"""Exercise the cloud command lifecycle without network calls or billable resources."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("cloud_commands", SCRIPTS / "cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class FakeCloud:
    token = "synthetic-api-token"

    def __init__(self, owner):
        self.resources = {
            "/linode/instances/101": {"id": 101, "label": "retail-demo-site", "tags": [owner]},
            "/networking/firewalls/202": {
                "id": 202,
                "label": "retail-demo-site-fw",
                "tags": [owner],
            },
        }
        self.deleted = []
        self.shared = False
        self.volume = False

    def request(self, path):
        if path not in self.resources:
            raise cloud.site.CloudError(404)
        return self.resources[path]

    def rows(self, path):
        if path.endswith("/devices"):
            if self.shared:
                return [{"entity": {"type": "linode", "id": 999}}]
            return (
                [{"entity": {"type": "linode_interface", "parent_entity": {"id": 101}}}]
                if "/linode/instances/101" in self.resources
                else []
            )
        if path.endswith("/volumes") and self.volume:
            return [{"id": 303}]
        return []

    def delete(self, connection, path, allowed):
        if path not in allowed:
            raise AssertionError("Unexpected deletion")
        self.deleted.append(path)
        self.resources.pop(path)


class CloudCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / ".local"
        self.folder = self.local / "sites/demo-site"
        self.folder.mkdir(parents=True)
        self.values = cloud.DEFAULTS | {"SITE_NAME": "demo-site"}
        self.owner = "a" * 32
        self.state = {
            "owner": self.owner,
            "instance": 101,
            "firewall": 202,
            "installed": True,
            "config": {
                "site": "demo-site",
                "region": "nl-ams",
                "admin_cidrs": ["8.8.8.8/32"],
                "ssh_key": str(self.folder / "client_ed25519"),
            },
        }
        self.save()
        for name, value in (("ROOT", self.root), ("LOCAL", self.local)):
            mocked = patch.object(cloud, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def save(self):
        (self.folder / "state.json").write_text(json.dumps(self.state))

    def test_configuration_is_data_and_cannot_execute_shell_or_hold_token(self):
        (self.root / ".env").write_text("SITE_NAME=demo-site\nADMIN_CIDR=\nLOCAL_PORT=8766\n")
        self.assertEqual(cloud.settings()["ADMIN_CIDR"], "")
        for value in (
            "SITE_NAME=$(touch /tmp/never-run)",
            "LINODE_TOKEN=synthetic",
            "LOCAL_PORT=22",
        ):
            (self.root / ".env").write_text(value + "\n")
            with self.assertRaises(ValueError):
                cloud.settings()

    def test_configuration_environment_overrides_allow_an_independent_site(self):
        with patch.dict(os.environ, {"SITE_NAME": "another-site", "LINODE_REGION": "us-sea"}):
            values = cloud.settings()
        self.assertEqual((values["SITE_NAME"], values["LINODE_REGION"]), ("another-site", "us-sea"))

    def test_audience_cannot_broaden_or_exclude_the_deployment_computer(self):
        self.assertEqual(cloud.audience("", "8.8.8.8/32"), ["8.8.8.8/32"])
        self.assertEqual(
            cloud.audience("1.1.1.1/32,8.8.8.8/32", "8.8.8.8/32"), ["1.1.1.1/32", "8.8.8.8/32"]
        )
        for value in ("0.0.0.0/0", "8.8.8.0/24", "127.0.0.1/32", "1.1.1.1/32", "8.8.8.8/32,"):
            with self.assertRaises(ValueError):
                cloud.audience(value, "8.8.8.8/32")

    def test_symlink_state_cannot_be_used(self):
        (self.folder / "state.json").unlink()
        (self.folder / "state.json").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "symlink"):
            cloud.folder_for(self.values)

    def test_declining_deployment_does_not_run_the_installer(self):
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        with (
            patch.object(cloud, "require_tools"),
            patch.object(cloud.site, "require_release_clearance"),
            patch.object(cloud, "capacity", return_value=True),
            patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
            patch("builtins.input", return_value="no"),
            patch.object(cloud, "installer") as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.deploy(self.values, connection)
        install.assert_not_called()

    def test_resume_uses_saved_key_audience_and_real_backend_then_warms_up(self):
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        remote = Mock()
        remote.run.return_value = SimpleNamespace(stdout="x" * 43 + "\n")
        (self.folder / "known_hosts").write_text("203.0.113.10 ssh-ed25519 synthetic\n")
        output = io.StringIO()
        with (
            patch.object(cloud, "require_tools"),
            patch.object(cloud.site, "require_release_clearance"),
            patch.object(cloud, "capacity", return_value=False),
            patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
            patch("builtins.input", return_value="deploy"),
            patch.object(cloud, "installer") as install,
            patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
            patch.object(cloud, "probe") as probe,
            patch.object(cloud, "init"),
            contextlib.redirect_stdout(output),
        ):
            cloud.deploy(self.values, connection)
        arguments, folder, _ = install.call_args.args
        self.assertEqual(folder, self.folder)
        self.assertIn("--demo-person", arguments)
        self.assertIn(self.state["config"]["ssh_key"], arguments)
        self.assertIn("8.8.8.8/32", arguments)
        self.assertIn("--accept-host-advisories", arguments)
        self.assertEqual(arguments[arguments.index("--gpu-type") + 1], cloud.DEFAULTS["GPU_TYPE"])
        self.assertEqual(arguments[arguments.index("--context-tokens") + 1], "16384")
        self.assertEqual(arguments[arguments.index("--gpu-memory-utilization") + 1], "0.7")
        probe.assert_called_once_with(self.values, "warmup")
        key = self.folder / "shopper-key.txt"
        self.assertEqual(key.read_text().strip(), "x" * 43)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("x" * 43, output.getvalue())
        self.assertIn("GPU public IPv4: 203.0.113.10", output.getvalue())
        self.assertIn("Browser URL: http://localhost:8766", output.getvalue())
        self.assertIn("make key", output.getvalue())
        receipt = self.folder / "deployment-timing.json"
        self.assertTrue(json.loads(receipt.read_text())["resumed"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("x" * 43, receipt.read_text())

    def test_wrong_region_and_changed_audience_fail_before_confirmation(self):
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        for values in (
            self.values | {"LINODE_REGION": "us-sea"},
            self.values | {"ADMIN_CIDR": "8.8.8.8/32,1.1.1.1/32"},
        ):
            with (
                patch.object(cloud, "require_tools"),
                patch.object(cloud.site, "require_release_clearance"),
                patch.object(cloud, "capacity", return_value=True),
                patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
                patch("builtins.input") as confirm,
            ):
                with self.assertRaises(ValueError):
                    cloud.deploy(values, connection)
                confirm.assert_not_called()

    def test_installer_streams_only_stages_and_rejects_failed_install(self):
        fake = self.root / "scripts"
        fake.mkdir()
        (fake / "site.py").write_text(
            "print('detail in private log')\nprint('[deploy] Stage one')\nraise SystemExit(7)\n"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), cloud.DeploymentProgress() as progress:
            with self.assertRaisesRegex(ValueError, "resume"):
                cloud.installer(["apply"], self.folder, progress)
        self.assertIn("elapsed | Stage one", output.getvalue())
        self.assertNotIn("detail in private log", output.getvalue())
        self.assertIn("detail in private log", (self.folder / "deploy.log").read_text())
        self.assertEqual((self.folder / "deploy.log").stat().st_mode & 0o777, 0o600)

    def test_quiet_installer_keeps_elapsed_updates_and_stops_timer_on_failure(self):
        fake = self.root / "scripts"
        fake.mkdir()
        (fake / "site.py").write_text(
            "import time\nprint('[deploy] Download models')\ntime.sleep(0.1)\nraise SystemExit(7)\n"
        )
        output = io.StringIO()
        with patch.object(cloud, "HEARTBEAT_SECONDS", 0.01), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "resume"):
                with cloud.DeploymentProgress() as progress:
                    cloud.installer(["apply"], self.folder, progress)
        self.assertIn("Still running: Download models", output.getvalue())
        self.assertIn("Deployment stopped after", output.getvalue())
        self.assertFalse(progress.thread.is_alive())
        self.assertFalse((self.folder / "deployment-timing.json").exists())

    def test_status_reads_saved_access_without_credentials_or_network_or_key_output(self):
        (self.folder / "known_hosts").write_text("203.0.113.10 ssh-ed25519 synthetic\n")
        (self.folder / "shopper-key.txt").write_text("x" * 43 + "\n")
        (self.folder / "deployment-timing.json").write_text(
            json.dumps(
                {
                    "owner": self.owner,
                    "elapsed_seconds": 3123,
                    "completed_at": "2026-11-07T10:00:00+00:00",
                    "resumed": False,
                }
            )
        )
        output = io.StringIO()
        with (
            patch.object(cloud.site, "Cloud") as api,
            patch.object(cloud, "remote_for") as remote,
            patch.object(sys, "argv", ["cloud.py", "status"]),
            patch.dict(os.environ, {"SITE_NAME": "demo-site"}),
            contextlib.redirect_stdout(output),
        ):
            cloud.main()
        api.assert_not_called()
        remote.assert_not_called()
        self.assertIn("203.0.113.10", output.getvalue())
        self.assertIn("52m 03s (fresh deployment)", output.getvalue())
        self.assertIn(str(self.folder / "shopper-key.txt"), output.getvalue())
        self.assertNotIn("x" * 43, output.getvalue())

    def test_explicit_key_command_copies_on_mac_and_displays_on_linux(self):
        remote = Mock()
        remote.run.return_value = SimpleNamespace(stdout="x" * 43 + "\n")
        for platform in ("darwin", "linux"):
            output = io.StringIO()
            with (
                patch.object(cloud.sys, "platform", platform),
                patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
                patch.object(cloud.shutil, "which", return_value="/usr/bin/pbcopy"),
                patch.object(cloud.subprocess, "run") as run,
                contextlib.redirect_stdout(output),
            ):
                cloud.copy_key(self.values)
            if platform == "darwin":
                self.assertNotIn("x" * 43, output.getvalue())
                run.assert_called_once_with(["pbcopy"], input="x" * 43, text=True, check=True)
            else:
                self.assertIn("x" * 43, output.getvalue())
                run.assert_not_called()

    def test_failed_warmup_does_not_print_ready_or_record_success(self):
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        remote = Mock()
        remote.run.return_value = SimpleNamespace(stdout="x" * 43 + "\n")
        output = io.StringIO()
        with (
            patch.object(cloud, "require_tools"),
            patch.object(cloud.site, "require_release_clearance"),
            patch.object(cloud, "capacity", return_value=True),
            patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
            patch("builtins.input", return_value="deploy"),
            patch.object(cloud, "installer"),
            patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
            patch.object(cloud, "probe", side_effect=ValueError("warmup failed")),
            patch.object(cloud, "init"),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaisesRegex(ValueError, "warmup failed"):
                cloud.deploy(self.values, connection)
        self.assertNotIn("Deployment ready", output.getvalue())
        self.assertFalse((self.folder / "deployment-timing.json").exists())

    def test_tunnel_is_loopback_only_and_uses_the_pinned_identity(self):
        remote = SimpleNamespace(options=["-o", "StrictHostKeyChecking=yes"], host="deploy@8.8.8.8")
        with (
            patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
            patch.object(cloud.socket, "socket"),
            patch.object(cloud.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.tunnel(self.values)
        arguments = run.call_args.args[0]
        self.assertIn("127.0.0.1:8766:127.0.0.1:8765", arguments)
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("ExitOnForwardFailure=yes", arguments)

    def test_destroy_removes_only_owned_ids_in_order_and_keeps_a_receipt(self):
        connection = FakeCloud(self.owner)
        with (
            patch.object(cloud, "delete_resource", side_effect=connection.delete),
            patch("builtins.input", return_value="retail-demo-site"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.destroy(self.values, connection)
        self.assertEqual(connection.deleted, ["/linode/instances/101", "/networking/firewalls/202"])
        self.assertFalse((self.folder / "state.json").exists())
        self.assertEqual(len(list(self.folder.glob("deleted-*.json"))), 1)

    def test_destroy_refuses_foreign_shared_or_newly_attached_resources(self):
        for kind in ("foreign", "shared", "volume", "pending"):
            self.state.pop("instance_pending", None)
            self.save()
            connection = FakeCloud(self.owner)
            if kind == "foreign":
                connection.resources["/linode/instances/101"]["tags"] = ["foreign"]
            elif kind == "shared":
                connection.shared = True
            elif kind == "volume":
                connection.volume = True
            else:
                self.state["instance_pending"] = True
                self.save()
            with (
                patch.object(cloud, "delete_resource") as delete,
                patch("builtins.input") as confirm,
            ):
                with self.assertRaises(ValueError):
                    cloud.destroy(self.values, connection)
                delete.assert_not_called()
                confirm.assert_not_called()

    def test_destroy_can_finish_after_instance_was_already_deleted(self):
        connection = FakeCloud(self.owner)
        connection.resources.pop("/linode/instances/101")
        with (
            patch.object(cloud, "delete_resource", side_effect=connection.delete),
            patch("builtins.input", return_value="retail-demo-site"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.destroy(self.values, connection)
        self.assertEqual(connection.deleted, ["/networking/firewalls/202"])

    def test_declined_delete_and_failed_api_leave_resume_state_intact(self):
        connection = FakeCloud(self.owner)
        with (
            patch.object(cloud, "delete_resource") as delete,
            patch("builtins.input", return_value="wrong"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.destroy(self.values, connection)
        delete.assert_not_called()
        self.assertTrue((self.folder / "state.json").exists())
        with (
            patch.object(cloud, "delete_resource", side_effect=OSError("interrupted")),
            patch("builtins.input", return_value="retail-demo-site"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(OSError):
                cloud.destroy(self.values, connection)
        self.assertTrue((self.folder / "state.json").exists())

    def test_delete_transport_rejects_any_path_outside_the_owned_set(self):
        with self.assertRaisesRegex(ValueError, "saved resource IDs"):
            cloud.delete_resource(Mock(), "/linode/instances/999", {"/linode/instances/101"})

    def test_saved_resource_ids_cannot_escape_the_resource_path(self):
        self.state["instance"] = "101/../../999"
        self.save()
        with self.assertRaisesRegex(ValueError, "invalid resource ID"):
            cloud.load_state(self.values)

    def test_address_discovery_forces_ipv4(self):
        with patch.object(
            cloud.subprocess, "run", return_value=SimpleNamespace(stdout="8.8.8.8\n")
        ) as run:
            self.assertEqual(cloud.public_ipv4(), "8.8.8.8/32")
        self.assertIn("-4", run.call_args.args[0])
        self.assertNotIn("-k", run.call_args.args[0])

    def test_lost_create_response_can_resume_even_when_capacity_is_gone(self):
        self.state.pop("instance")
        self.state["instance_pending"] = True
        self.save()
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        remote = Mock()
        remote.run.return_value = SimpleNamespace(stdout="x" * 43 + "\n")
        with (
            patch.object(cloud, "require_tools"),
            patch.object(cloud.site, "require_release_clearance"),
            patch.object(cloud, "capacity", return_value=False),
            patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
            patch("builtins.input", return_value="deploy"),
            patch.object(cloud, "installer") as install,
            patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
            patch.object(cloud, "probe"),
            patch.object(cloud, "init"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.deploy(self.values, connection)
        install.assert_called_once()
        self.assertIn("apply", install.call_args.args[0])

    def test_invalid_runtime_settings_fail_without_cloud_access(self):
        for name, value in (
            ("MODEL_PROFILE", "compact"),
            ("VLLM_MAX_MODEL_LEN", "0"),
            ("VLLM_MAX_MODEL_LEN", "65536"),
            ("VLLM_MAX_MODEL_LEN", "16.5"),
            ("VLLM_GPU_MEMORY_UTILIZATION", "nan"),
            ("VLLM_GPU_MEMORY_UTILIZATION", "0.9"),
            ("TRYON_ENABLED", "perhaps"),
        ):
            with patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                cloud.settings()
        for method in (cloud.deploy, cloud.gpu_check):
            connection = Mock()
            for plan in ("g2-gpu-rtx4000a1-m", "g3-gpu-rtxpro6000-blackwell-2"):
                with self.assertRaisesRegex(ValueError, "not supported"):
                    method(self.values | {"GPU_TYPE": plan}, connection)
            connection.request.assert_not_called()
            connection.rows.assert_not_called()

    def test_resume_rejects_changed_runtime_before_install_or_confirmation(self):
        connection = Mock()
        for overrides in (
            {"VLLM_MAX_MODEL_LEN": "8192"},
            {"VLLM_GPU_MEMORY_UTILIZATION": "0.65"},
            {"TRYON_ENABLED": "false"},
        ):
            with (
                patch.object(cloud, "require_tools"),
                patch.object(cloud.site, "require_release_clearance"),
                patch("builtins.input") as confirm,
                patch.object(cloud, "installer") as install,
                self.assertRaisesRegex(ValueError, "cannot change on resume"),
            ):
                cloud.deploy(self.values | overrides, connection)
            confirm.assert_not_called()
            install.assert_not_called()

    def test_fresh_site_passes_custom_runtime_and_disabled_tryon_to_installer(self):
        (self.folder / "state.json").unlink()
        (self.folder / "client_ed25519").write_text("synthetic fixture")
        (self.folder / "known_hosts").write_text("203.0.113.10 ssh-ed25519 synthetic\n")
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        remote = Mock()
        remote.run.return_value = SimpleNamespace(stdout="x" * 43 + "\n")
        self.state["config"].update(
            context_tokens=8192,
            gpu_memory_utilization=0.65,
            model_profile="retail-full",
            demo_person=False,
        )
        with (
            patch.object(cloud, "require_tools"),
            patch.object(cloud.site, "require_release_clearance"),
            patch.object(cloud, "capacity", return_value=True) as capacity,
            patch.object(cloud, "public_ipv4", return_value="8.8.8.8/32"),
            patch("builtins.input", return_value="deploy"),
            patch.object(cloud, "installer", side_effect=lambda *args: self.save()) as install,
            patch.object(cloud, "remote_for", return_value=(self.folder, remote)),
            patch.object(cloud, "probe"),
            patch.object(cloud, "init"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cloud.deploy(
                self.values
                | {
                    "VLLM_MAX_MODEL_LEN": "8192",
                    "VLLM_GPU_MEMORY_UTILIZATION": "0.65",
                    "TRYON_ENABLED": "false",
                },
                connection,
            )
        capacity.assert_called_once_with(connection, "nl-ams", cloud.DEFAULTS["GPU_TYPE"])
        args = install.call_args.args[0]
        self.assertNotIn("--demo-person", args)
        self.assertEqual(args[args.index("--context-tokens") + 1], "8192")
        self.assertEqual(args[args.index("--gpu-memory-utilization") + 1], "0.65")

    def test_gpu_check_uses_selected_plan_for_price_and_capacity(self):
        connection = Mock()
        connection.request.return_value = {"price": {"hourly": 3.6}}
        connection.rows.side_effect = lambda path: (
            [{"id": "nl-ams", "capabilities": ["GPU Linodes"]}]
            if path == "/regions"
            else [{"plan": self.values["GPU_TYPE"], "available": True}]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cloud.gpu_check(self.values, connection)
        connection.request.assert_called_once_with("/linode/types/" + self.values["GPU_TYPE"])
        self.assertIn("nl-ams", output.getvalue())
        self.assertIn("16384 tokens", output.getvalue())


if __name__ == "__main__":
    unittest.main()
