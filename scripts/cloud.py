"""The cloud demo's Make commands. Private settings and receipts stay on this computer."""

import argparse
import concurrent.futures
import fcntl
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

import deployment_profile as profile
from demo import LOCAL, ROOT, init, private_write

spec = importlib.util.spec_from_file_location("site_deploy", ROOT / "scripts/site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
DEFAULTS = {
    "SITE_NAME": "retail-demo",
    "LINODE_REGION": "nl-ams",
    "ADMIN_CIDR": "",
    "LOCAL_PORT": "8766",
    "GPU_TYPE": profile.GPU_TYPE,
    "MODEL_PROFILE": profile.MODEL_PROFILE,
    "VLLM_MAX_MODEL_LEN": str(profile.CONTEXT_TOKENS),
    "VLLM_GPU_MEMORY_UTILIZATION": str(profile.MEMORY_FRACTION),
    "TRYON_ENABLED": "true",
}
HEARTBEAT_SECONDS = 30


def duration(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}h " if hours else "") + f"{minutes:02d}m {seconds:02d}s"


class DeploymentProgress:
    """Keep long, quiet install steps visible without printing private logs."""

    def __init__(self):
        self.started = time.monotonic()
        self.current = "Prepare deployment"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)

    def elapsed(self):
        return time.monotonic() - self.started

    def stage(self, message):
        self.current = message
        print(f"[deploy] {duration(self.elapsed())} elapsed | {message}", flush=True)

    def heartbeat(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            print(
                f"[deploy] {duration(self.elapsed())} elapsed | Still running: {self.current}",
                flush=True,
            )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, kind, error, traceback):
        self.stop.set()
        self.thread.join()
        if kind is not None:
            print(
                f"Deployment stopped after {duration(self.elapsed())} during: {self.current}",
                flush=True,
            )


def settings():
    path = ROOT / ".env"
    if path.is_symlink():
        raise ValueError("Refusing a symlink for .env")
    values = dict(DEFAULTS)
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in DEFAULTS:
                raise ValueError(
                    "Use only the cloud settings in .env.example; keep LINODE_TOKEN in your shell"
                )
            parts = shlex.split(value, comments=True)
            if len(parts) > 1:
                raise ValueError(f"Invalid {key} in .env")
            values[key] = parts[0] if parts else ""
    values.update({key: os.environ[key] for key in DEFAULTS if key in os.environ})
    for key in ("SITE_NAME", "LINODE_REGION"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", values[key]):
            raise ValueError(f"{key} must contain 3 to 41 lowercase letters, digits or hyphens")
    if not values["LOCAL_PORT"].isdigit() or not 1024 <= int(values["LOCAL_PORT"]) <= 65535:
        raise ValueError("LOCAL_PORT must be between 1024 and 65535")
    selected_runtime(values)
    values["TRYON_ENABLED"] = values["TRYON_ENABLED"].lower()
    if values["TRYON_ENABLED"] not in {"true", "false"}:
        raise ValueError("TRYON_ENABLED must be true or false")
    return values


def selected_runtime(values):
    return profile.runtime_settings(
        values["MODEL_PROFILE"],
        values["VLLM_MAX_MODEL_LEN"],
        values["VLLM_GPU_MEMORY_UTILIZATION"],
    )


def folder_for(values, create=False):
    folder = LOCAL / "sites" / values["SITE_NAME"]
    if any(p.is_symlink() for p in (LOCAL, LOCAL / "sites", folder, folder / "state.json")):
        raise ValueError("Refusing a symlink for private site state")
    if create:
        init()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    return folder


def environment():
    values = settings()
    init()
    if not (ROOT / ".env").exists():
        private_write(ROOT / ".env", (ROOT / ".env.example").read_text())
    (ROOT / ".env").chmod(0o600)
    print("Cloud settings: .env. Defaults: Amsterdam, one 96 GB GPU, fictional try-on person.")
    print(f"Site: {values['SITE_NAME']}. Next: make gpu-check")


def require_tools():
    if sys.version_info[:2] != (3, 12) or os.name != "posix":
        raise ValueError("Use Python 3.12 on macOS or Linux")
    for command in ("ssh", "scp", "ssh-keygen", "curl"):
        if not shutil.which(command):
            raise ValueError(f"Install {command} before deploying")


def public_ipv4():
    result = subprocess.run(
        [
            "curl",
            "-4",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "https://whatismyip.akamai.com",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    address = ipaddress.IPv4Address(result.stdout.strip())
    if not address.is_global:
        raise ValueError("Could not detect a public IPv4 address")
    return str(address) + "/32"


def audience(raw, detected):
    addresses = [item.strip() for item in raw.split(",")] if raw else [detected]
    for value in addresses:
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError(
                "ADMIN_CIDR must contain public IPv4 /32 addresses separated by commas"
            )
    if detected not in addresses:
        raise ValueError(f"Add this computer's {detected} to ADMIN_CIDR before deploying")
    return sorted(set(addresses))


def capacity(cloud, region, gpu_type):
    rows = cloud.rows(f"/regions/{region}/availability")
    return any(row["plan"] == gpu_type and row["available"] for row in rows)


def price(plan, region):
    return next(
        (row for row in plan.get("region_prices", []) if row["id"] == region), plan["price"]
    )


def gpu_check(values, cloud):
    profile.validate_gpu(values["GPU_TYPE"])
    runtime = selected_runtime(values)
    plan = cloud.request("/linode/types/" + values["GPU_TYPE"])
    regions = [
        r
        for r in cloud.rows("/regions")
        if "GPU Linodes" in r.get("capabilities", []) or r["id"] == values["LINODE_REGION"]
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            r["id"]: pool.submit(capacity, cloud, r["id"], values["GPU_TYPE"]) for r in regions
        }
        available = {region: future.result() for region, future in futures.items()}
    print(f"{values['GPU_TYPE']}: one RTX PRO 6000 Blackwell, 96 GB VRAM")
    print_runtime(runtime, values["TRYON_ENABLED"] == "true")
    print(f"{'Region':18} {'Available':10} USD/hour")
    for region, ready in sorted(available.items()):
        marker = "  <- LINODE_REGION" if region == values["LINODE_REGION"] else ""
        print(
            f"{region:18} {('yes' if ready else 'no'):10} {price(plan, region)['hourly']}{marker}"
        )
    print("Set LINODE_REGION in .env to an available region. This command creates nothing.")


def load_state(values):
    folder = folder_for(values)
    path = folder / "state.json"
    if not path.is_file():
        raise ValueError("No saved deployment for SITE_NAME. Run make deploy first")
    state = json.loads(path.read_text())
    if state["config"]["site"] != values["SITE_NAME"] or not re.fullmatch(
        r"[a-f0-9]{32}", state["owner"]
    ):
        raise ValueError("Private site state is invalid")
    for name in ("instance", "firewall"):
        if name in state and (type(state[name]) is not int or state[name] <= 0):
            raise ValueError("Private state contains an invalid resource ID")
    return folder, state


def saved_address(folder):
    hosts = folder / "known_hosts"
    if hosts.is_symlink() or not hosts.is_file():
        raise ValueError("Restore the pinned known_hosts file from private site state")
    fields = hosts.read_text().split()
    if not fields:
        raise ValueError("The pinned known_hosts file is empty")
    return str(ipaddress.IPv4Address(fields[0]))


def remote_for(values):
    folder, state = load_state(values)
    address = saved_address(folder)
    remote = site.Remote(state["config"], folder, address, state["owner"])
    remote.run("true", capture=True)
    return folder, remote


def installer(arguments, folder, progress):
    log = folder / "deploy.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    print(f"Installation log: {log}")
    print("Elapsed time updates every 30 seconds. Keep this terminal open.", flush=True)
    with os.fdopen(descriptor, "a") as output:
        with subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "scripts/site.py"), *arguments],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            try:
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    if line.startswith("[deploy] "):
                        progress.stage(line.strip().removeprefix("[deploy] "))
                code = process.wait()
            except KeyboardInterrupt:
                process.terminate()
                raise
    if code:
        raise ValueError(
            f"Installation stopped. See {log}, then rerun make deploy to resume. Created resources remain billable"
        )


def sync_key(folder, remote):
    result = remote.run(
        "cd /home/deploy/retail-app\n.venv/bin/python -c \"import sys; sys.path.insert(0, 'scripts'); from demo import read_env; print(read_env()['TENANT_A_KEY'])\"",
        capture=True,
    )
    key = result.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        raise ValueError("Could not retrieve a valid shopper key")
    private_write(folder / "shopper-key.txt", key + "\n")


def probe(values, command):
    _, remote = remote_for(values)
    remote.run("cd /home/deploy/retail-app\n.venv/bin/python scripts/cloud_probe.py " + command)


def access_details(values):
    folder, state = load_state(values)
    print(f"Site: retail-{state['config']['site']} ({state['config']['region']})")
    print("GPU plan: " + state["config"].get("gpu_type", profile.GPU_TYPE))
    print_runtime(
        profile.with_runtime_defaults(state["config"]),
        state["config"].get("demo_person", True) or bool(state["config"].get("reference_sha256")),
    )
    if state.get("instance"):
        print(f"Instance ID: {state['instance']}")
    if (folder / "known_hosts").exists() or (folder / "known_hosts").is_symlink():
        print("GPU public IPv4: " + saved_address(folder))
    else:
        print("GPU public IPv4: not recorded yet")
    print("Browser URL: http://localhost:" + values["LOCAL_PORT"] + " (after make tunnel)")
    path = folder / "shopper-key.txt"
    if path.is_symlink():
        raise ValueError("Refusing a symlink for the shopper API key")
    if path.is_file():
        print(f"App API key: {path}")
        print("Run make key: copies to the clipboard on macOS; displays the key on Linux.")
    else:
        print("App API key: not saved yet. Run make key once the app is installed.")
    print("Use the app API key in the sign-in form, not your LINODE_TOKEN.")
    print("The public IP is for SSH. Direct browser access requires a domain with HTTPS.")
    print("The GPU bills until make destroy. Closing the tunnel does not stop billing.")


def status(values):
    print("Saved deployment details. Run make cloud-check to check live service readiness.")
    access_details(values)
    folder, state = load_state(values)
    timing = folder / "deployment-timing.json"
    if timing.is_symlink():
        raise ValueError("Refusing a symlink for the deployment timing receipt")
    if timing.is_file():
        receipt = json.loads(timing.read_text())
        if receipt.get("owner") != state["owner"]:
            return
        print(
            f"Last successful run: {duration(receipt['elapsed_seconds'])} "
            f"({'resume' if receipt['resumed'] else 'fresh deployment'}), "
            f"finished {receipt['completed_at']}"
        )


def print_runtime(runtime, tryon):
    print(
        f"Model profile: {runtime['model_profile']}; context: {runtime['context_tokens']} tokens; "
        f"vLLM GPU budget: {runtime['gpu_memory_utilization']:.0%}; "
        f"FP8 KV cache; try-on: {'enabled' if tryon else 'disabled'}"
    )
    if (runtime["context_tokens"], runtime["gpu_memory_utilization"]) != (
        profile.CONTEXT_TOKENS,
        profile.MEMORY_FRACTION,
    ):
        print(
            "Custom memory/context settings have not been hardware-validated. Run make smoke after deployment."
        )


def deploy(values, cloud):
    profile.validate_gpu(values["GPU_TYPE"])
    runtime = selected_runtime(values)
    require_tools()
    site.require_release_clearance()
    plan = cloud.request("/linode/types/" + values["GPU_TYPE"])
    folder = folder_for(values)
    saved = (
        json.loads((folder / "state.json").read_text())
        if (folder / "state.json").exists()
        else None
    )
    if saved and saved["config"]["region"] != values["LINODE_REGION"]:
        raise ValueError(
            "An existing site's region cannot change. Set a new SITE_NAME for another region"
        )
    if saved:
        existing = profile.with_runtime_defaults(saved["config"])
        if (
            existing.get("gpu_type", profile.GPU_TYPE) != values["GPU_TYPE"]
            or any(existing[name] != value for name, value in runtime.items())
            or existing.get("demo_person", True) != (values["TRYON_ENABLED"] == "true")
        ):
            raise ValueError(
                "Saved GPU, model, context, memory and try-on settings cannot change on resume. "
                "Restore the original settings or use a new SITE_NAME."
            )
    if not capacity(cloud, values["LINODE_REGION"], values["GPU_TYPE"]) and not (
        saved and (saved.get("instance") or saved.get("instance_pending"))
    ):
        raise ValueError(
            "No GPU capacity in LINODE_REGION. Run make gpu-check and select an available region"
        )
    detected = public_ipv4()
    addresses = audience(
        values["ADMIN_CIDR"] or (",".join(saved["config"]["admin_cidrs"]) if saved else ""),
        detected,
    )
    if saved and addresses != saved["config"]["admin_cidrs"]:
        raise ValueError(
            "Saved firewall addresses cannot change through deploy. Keep ADMIN_CIDR unchanged"
        )
    print(
        f"Site: {values['SITE_NAME']} in {values['LINODE_REGION']}; {price(plan, values['LINODE_REGION'])['hourly']} USD/hour"
    )
    print("One 96 GB GPU and one dedicated firewall. Allowed IPv4: " + ", ".join(addresses))
    print_runtime(runtime, values["TRYON_ENABLED"] == "true")
    print("Estimated first deployment: 45-90 minutes. Planning estimate; total not measured yet.")
    print(
        "Downloads, builds and scans vary. First model compilation took about 15 minutes in the prior run."
    )
    if saved:
        print(
            "Resuming this site reuses completed steps; the first-deployment estimate does not apply."
        )
    print(
        "Review THIRD_PARTY_NOTICES.md for model licenses and SECURITY.md for unfixed Ubuntu advisories."
    )
    print(
        "Typing deploy accepts those licenses and the restricted-demo host exception. Dependency and container findings still block installation."
    )
    if input("Type deploy to create or resume this paid site: ").strip() != "deploy":
        print("Cancelled. No cloud resources changed.")
        return
    folder = folder_for(values, create=True)
    key = folder / "client_ed25519"
    if saved:
        key = saved["config"]["ssh_key"]
    elif key.is_symlink() or key.with_suffix(".pub").is_symlink():
        raise ValueError("Refusing a symlink for the deployment SSH key")
    elif not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    arguments = [
        "apply",
        "--site",
        values["SITE_NAME"],
        "--region",
        values["LINODE_REGION"],
        "--gpu-type",
        values["GPU_TYPE"],
        "--ssh-key",
        str(key),
        "--accept-model-licenses",
        "--accept-host-advisories",
    ]
    arguments.extend(profile.arguments(runtime))
    if values["TRYON_ENABLED"] == "true":
        arguments.append("--demo-person")
    for address in addresses:
        arguments.extend(["--admin-cidr", address])
    with DeploymentProgress() as progress:
        installer(arguments, folder, progress)
        progress.stage("Save the app API key")
        folder, remote = remote_for(values)
        sync_key(folder, remote)
        progress.stage("Warm up speech and the language model with a synthetic request")
        probe(values, "warmup")
        elapsed = round(progress.elapsed(), 1)
        private_write(
            folder / "deployment-timing.json",
            json.dumps(
                {
                    "owner": load_state(values)[1]["owner"],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": elapsed,
                    "resumed": bool(saved),
                    "scope": "This invocation: installation, key retrieval and voice warmup. Excludes confirmation and prior attempts.",
                },
                indent=2,
            )
            + "\n",
        )
    print(f"Deployment ready. Actual time for this run: {duration(elapsed)}.")
    access_details(values)
    print(
        "Next: make tunnel, then make key in a second terminal. Use make status to see these details again."
    )


def tunnel(values):
    _, remote = remote_for(values)
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", int(values["LOCAL_PORT"])))
        except OSError:
            raise ValueError("LOCAL_PORT is already in use. Set another port in .env") from None
    print(
        f"Open http://localhost:{values['LOCAL_PORT']}. Keep this terminal open; Ctrl+C closes the tunnel.",
        flush=True,
    )
    subprocess.run(
        [
            "ssh",
            *remote.options,
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-N",
            "-L",
            f"127.0.0.1:{values['LOCAL_PORT']}:127.0.0.1:8765",
            remote.host,
        ],
        check=True,
    )


def copy_key(values):
    folder, remote = remote_for(values)
    sync_key(folder, remote)
    path = folder / "shopper-key.txt"
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=path.read_text().strip(), text=True, check=True)
        print("Shopper key copied. Paste it into the sign-in form.")
    else:
        print("App API key. Paste this into the sign-in form:")
        print(path.read_text().strip())


def optional(cloud, path):
    try:
        return cloud.request(path)
    except site.CloudError as error:
        if error.status == 404:
            return None
        raise


def owns(row, label, owner):
    if row is not None and (row.get("label") != label or owner not in row.get("tags", [])):
        raise ValueError("Resource ownership differs from the saved site; refusing deletion")


def device_owner(device):
    entity = device["entity"]
    return (
        entity.get("parent_entity", {}).get("id")
        if entity["type"] == "linode_interface"
        else entity.get("id")
        if entity["type"] == "linode"
        else None
    )


def delete_resource(cloud, path, allowed):
    if path not in allowed:
        raise ValueError("Deletion is limited to this site's saved resource IDs")
    request = urllib.request.Request(
        "https://api.linode.com/v4" + path,
        method="DELETE",
        headers={"Authorization": "Bearer " + cloud.token},
    )
    try:
        with cloud.opener.open(request, timeout=90):
            pass
    except urllib.error.HTTPError as error:
        raise ValueError(
            f"Deletion returned HTTP {error.code}; retained state can be resumed"
        ) from None
    for _ in range(60):
        if optional(cloud, path) is None:
            return
        time.sleep(3)
    raise ValueError("Deletion is still pending. Rerun make destroy to verify completion")


@contextmanager
def site_lock(folder):
    descriptor = os.open(folder / "apply.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w"):
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def destroy(values, cloud):
    folder, _ = load_state(values)
    with site_lock(folder):
        _, state = load_state(values)
        if state.get("instance_pending") or state.get("firewall_pending"):
            raise ValueError(
                "A cloud creation has an uncertain result. Inspect the private state before deleting"
            )
        label = "retail-" + state["config"]["site"]
        paths = {
            kind: path + str(state[kind])
            for kind, path in (
                ("instance", "/linode/instances/"),
                ("firewall", "/networking/firewalls/"),
            )
            if state.get(kind)
        }
        current = {kind: optional(cloud, path) for kind, path in paths.items()}
        for kind, row in current.items():
            owns(row, label + ("-fw" if kind == "firewall" else ""), state["owner"])
        if current.get("instance"):
            for resource in ("volumes", "nodebalancers"):
                if cloud.rows(paths["instance"] + "/" + resource):
                    raise ValueError(
                        "Additional resources are attached. Review them before deletion"
                    )
            if any(
                r.get("reserved") or r.get("reserved_id")
                for r in cloud.rows("/networking/ips")
                if r.get("linode_id") == state["instance"]
            ):
                raise ValueError("A reserved IP is attached. Review it before deletion")
        if current.get("firewall"):
            devices = cloud.rows(paths["firewall"] + "/devices")
            if any(device_owner(d) != state.get("instance") for d in devices):
                raise ValueError("The firewall is attached to another resource; refusing deletion")
        print(
            f"Delete {label}, its local disks, and its dedicated firewall. No other site is selected."
        )
        if input("Type the full instance label to delete: ").strip() != label:
            print("Cancelled. No resources deleted.")
            return
        for kind in ("instance", "firewall"):
            if current.get(kind):
                row = optional(cloud, paths[kind])
                owns(row, label + ("-fw" if kind == "firewall" else ""), state["owner"])
                if kind == "firewall" and cloud.rows(paths[kind] + "/devices"):
                    raise ValueError(
                        "Firewall still has attached devices. Rerun make destroy after deletion completes"
                    )
                if row is not None:
                    delete_resource(cloud, paths[kind], set(paths.values()))
        if any(optional(cloud, path) is not None for path in paths.values()):
            raise ValueError("Could not confirm deletion. Private state retained")
        receipt = {
            "deleted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "site": state["config"]["site"],
            "resources": paths,
            "confirmed_absent": True,
        }
        private_write(
            folder / f"deleted-{time.time_ns()}.json", json.dumps(receipt, indent=2) + "\n"
        )
        (folder / "state.json").unlink()
        (folder / "shopper-key.txt").unlink(missing_ok=True)
        print(
            "Deleted this site's GPU and dedicated firewall. Private verification records are retained."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "env",
            "gpu-check",
            "deploy",
            "tunnel",
            "key",
            "status",
            "check",
            "smoke",
            "destroy",
            "logs",
        ],
    )
    args = parser.parse_args()
    values = settings()
    if args.command == "env":
        environment()
    elif args.command in ("gpu-check", "deploy", "destroy"):
        {"gpu-check": gpu_check, "deploy": deploy, "destroy": destroy}[args.command](
            values, site.Cloud()
        )
    elif args.command in ("check", "smoke"):
        probe(values, args.command)
    elif args.command == "logs":
        _, remote = remote_for(values)
        remote.run(
            "sudo journalctl -u retail-vlm -u retail-speech -u retail-tryon -u retail-gateway -n 60 --no-pager"
        )
    else:
        {"tunnel": tunnel, "key": copy_key, "status": status}[args.command](values)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("Stopped. Cloud resources remain until make destroy.") from None
