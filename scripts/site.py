"""Plan or create one independent GPU site. Existing resources are never updated or deleted."""

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from demo import LOCAL, ROOT, init, private_write
from public_check import demo_reference
from release import export
from security import dependency_audit, require_release_clearance
from security_tools import ensure_auditor


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Cloud API redirects are refused")


class CloudError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Cloud API returned HTTP {status}; inspect the account privately")


class Cloud:
    def __init__(self):
        self.token = os.environ.get("LINODE_TOKEN", "")
        if not self.token:
            raise ValueError("Set LINODE_TOKEN privately in your environment")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None):
        if payload is not None and path not in {"/networking/firewalls", "/linode/instances"}:
            raise ValueError("Only creation of new resources is supported")
        request = urllib.request.Request(
            "https://api.linode.com/v4" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise CloudError(exc.code) from None

    def rows(self, path):
        first = self.request(path + "?page_size=100")
        # Region availability is an unpaginated array; resource collections are paginated.
        if isinstance(first, list):
            return first
        values = first["data"]
        for page in range(2, first["pages"] + 1):
            values.extend(self.request(path + f"?page_size=100&page={page}")["data"])
        return values


def configuration(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", args.site):
        raise ValueError("Use a lowercase site name of 3 to 41 characters")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", args.region):
        raise ValueError("Use a Linode region ID")
    key = Path(args.ssh_key).expanduser().resolve()
    public = Path(str(key) + ".pub")
    if not key.is_file() or not public.is_file() or key.stat().st_mode & 0o077:
        raise ValueError(
            "Use an existing private SSH key with owner-only permissions and a .pub file"
        )
    public_key = public.read_text().strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", public_key):
        raise ValueError("Use an Ed25519 SSH key")
    decoded = base64.b64decode(public_key.split()[1], validate=True)
    if len(decoded) != 51 or not decoded.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
        raise ValueError("Invalid Ed25519 public key")
    addresses = sorted(set(args.admin_cidr))
    for value in addresses:
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
            raise ValueError("Use each administrator's public IPv4 address with /32")
    if not re.fullmatch(r"[a-z0-9-]+", args.gpu_type):
        raise ValueError("Invalid GPU plan ID")
    if args.reference or args.demo_person:
        reference = (
            demo_reference(ROOT)
            if args.demo_person
            else Path(args.reference).expanduser().resolve()
        )
        if (
            not reference.is_file()
            or reference.suffix.lower() not in {".jpg", ".jpeg", ".png"}
            or reference.stat().st_size > 2 * 1024 * 1024
        ):
            raise ValueError("Use your permitted JPEG or PNG reference under 2 MB")
    return {
        "site": args.site,
        "region": args.region,
        "gpu_type": args.gpu_type,
        "ssh_key": str(key),
        "public_key": public_key,
        "admin_cidrs": addresses,
        "tenant": args.tenant,
        "demo_person": args.demo_person,
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest()
        if args.reference or args.demo_person
        else None,
    }


def firewall_rules(config):
    return {
        "inbound_policy": "DROP",
        "outbound_policy": "ACCEPT",
        "outbound": [],
        "inbound": [
            {
                "label": "administrators",
                "action": "ACCEPT",
                "protocol": "TCP",
                "ports": "22,80,443",
                "addresses": {"ipv4": config["admin_cidrs"], "ipv6": []},
            }
        ],
    }


def verify_firewall(actual, config):
    expected = firewall_rules(config)
    if actual.get("inbound_policy") != "DROP" or actual.get("outbound_policy") != "ACCEPT":
        raise ValueError("Firewall policy changed; refusing deployment")
    inbound = actual.get("inbound", [])
    if len(inbound) != 1 or actual.get("outbound"):
        raise ValueError("Firewall rules changed; refusing deployment")
    rule = inbound[0]
    wanted = expected["inbound"][0]
    if any(rule.get(key) != wanted[key] for key in ("action", "protocol", "ports")):
        raise ValueError("Firewall ports changed; refusing deployment")
    addresses = rule.get("addresses", {})
    if sorted(addresses.get("ipv4", [])) != config["admin_cidrs"] or addresses.get("ipv6"):
        raise ValueError("Firewall audience changed; refusing deployment")


def ensure_resource(cloud, path, label, payload, state, name, save):
    found = [row for row in cloud.rows(path) if row["label"] == label]
    if len(found) > 1:
        raise ValueError("Duplicate resource labels require manual review")
    if found:
        row = found[0]
        if state["owner"] not in row.get("tags", []) or (
            state.get(name) is not None and state[name] != row["id"]
        ):
            raise ValueError("Refusing an existing resource owned by another deployment")
    else:
        if state.get(name) is not None or state.get(name + "_pending"):
            raise ValueError(
                "Previous creation has an uncertain result; inspect the account before retrying"
            )
        state[name + "_pending"] = True
        save()
        row = cloud.request(path, payload)
    state[name], state[name + "_pending"] = row["id"], False
    save()
    return row


def cloud_config(owner, config, host_private, host_public):
    return {
        "hostname": "retail-" + config["site"],
        "manage_etc_hosts": True,
        "disable_root": True,
        "ssh_pwauth": False,
        "ssh_keys": {"ed25519_private": host_private, "ed25519_public": host_public},
        "users": [
            {
                "name": "deploy",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "ssh_authorized_keys": [config["public_key"]],
            }
        ],
        "write_files": [
            {"path": "/etc/retail-voice-owner", "permissions": "0644", "content": owner + "\n"},
            {
                "path": "/etc/ssh/sshd_config.d/00-retail-security.conf",
                "permissions": "0600",
                "content": "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nPermitTunnel no\nAllowUsers deploy\n",
            },
        ],
        "runcmd": [["systemctl", "restart", "ssh"]],
    }


class Remote:
    def __init__(self, config, folder, address, owner):
        self.host = "deploy@" + str(ipaddress.IPv4Address(address))
        self.options = [
            "-i",
            config["ssh_key"],
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(folder / "known_hosts"),
        ]
        self.owner = owner

    def run(self, script, check=True, capture=False):
        guard = (
            'test "$(cat /etc/retail-voice-owner)" = ' + shlex.quote(self.owner) + " || exit 91\n"
        )
        return subprocess.run(
            ["ssh", *self.options, self.host, "bash -se"],
            input=guard + script + "\n",
            text=True,
            check=check,
            capture_output=capture,
        )

    def upload(self, path, name):
        if not re.fullmatch(r"[a-z0-9.-]+", name):
            raise ValueError("Unsafe upload name")
        self.run("true", capture=True)
        subprocess.run(
            ["scp", *self.options, str(path), self.host + ":/home/deploy/" + name], check=True
        )

    def wait(self, old_boot=None):
        for _ in range(120):
            result = self.run("cat /proc/sys/kernel/random/boot_id", check=False, capture=True)
            if result.returncode == 0 and (old_boot is None or result.stdout.strip() != old_boot):
                return
            time.sleep(5)
        raise ValueError("SSH startup timed out; rerun with the same site to resume")


def apply(cloud, config, args, folder, available=True):
    state_path = folder / "state.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"owner": secrets.token_hex(16), "config": config}
    )
    if state["config"] != config:
        raise ValueError(
            "Site inputs are immutable; use a different site name for another deployment"
        )
    if not available and not (state.get("instance") or state.get("instance_pending")):
        raise ValueError("No capacity for a new GPU in the selected region")

    def save():
        private_write(state_path, json.dumps(state, indent=2) + "\n")

    save()
    print("[deploy] Audit dependencies", flush=True)
    ensure_auditor()
    dependency_audit()
    host_key = folder / "host_ed25519"
    if not host_key.exists():
        if state.get("instance") or state.get("instance_pending"):
            raise ValueError(
                "Restore the original private site state; do not replace its SSH identity"
            )
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True
        )
    host_public = Path(str(host_key) + ".pub").read_text().strip()
    label = "retail-" + config["site"]
    print("[deploy] Create or verify the dedicated firewall", flush=True)
    firewall = ensure_resource(
        cloud,
        "/networking/firewalls",
        label + "-fw",
        {"label": label + "-fw", "tags": [state["owner"]], "rules": firewall_rules(config)},
        state,
        "firewall",
        save,
    )
    if firewall.get("status") != "enabled":
        raise ValueError("The site's firewall must be enabled before proceeding")
    verify_firewall(cloud.request(f"/networking/firewalls/{firewall['id']}/rules"), config)
    cloud_data = cloud_config(state["owner"], config, host_key.read_text(), host_public)
    print("[deploy] Create or verify the GPU", flush=True)
    instance = ensure_resource(
        cloud,
        "/linode/instances",
        label,
        {
            "label": label,
            "region": config["region"],
            "type": config["gpu_type"],
            "image": "linode/ubuntu24.04",
            "authorized_keys": [config["public_key"]],
            "booted": True,
            "backups_enabled": False,
            "disk_encryption": "enabled",
            "tags": [state["owner"]],
            "interface_generation": "linode",
            "interfaces": [
                {
                    "public": {"ipv4": {"addresses": [{"primary": True}]}},
                    "firewall_id": firewall["id"],
                }
            ],
            "metadata": {
                "user_data": base64.b64encode(
                    ("#cloud-config\n" + json.dumps(cloud_data)).encode()
                ).decode()
            },
        },
        state,
        "instance",
        save,
    )
    if instance["region"] != config["region"] or instance["type"] != config["gpu_type"]:
        raise ValueError("Cloud instance differs from the private deployment state")
    address = next(ip for ip in instance["ipv4"] if ipaddress.ip_address(ip).is_global)
    private_write(folder / "known_hosts", address + " " + host_public + "\n")
    print(f"[deploy] GPU public IPv4: {address}", flush=True)
    remote = Remote(config, folder, address, state["owner"])
    print("[deploy] Wait for pinned SSH access", flush=True)
    remote.wait()
    if not state.get("bootstrapped"):
        print("[deploy] Install Ubuntu updates, GPU driver and container tools", flush=True)
        remote.upload(ROOT / "deploy/bootstrap.py", "retail-bootstrap.py")
        command = ["sudo", "python3", "/home/deploy/retail-bootstrap.py", "--owner", state["owner"]]
        for cidr in config["admin_cidrs"]:
            command.extend(["--admin-cidr", cidr])
        remote.run(shlex.join(command))
        state["bootstrapped"] = True
        save()
    if not state.get("rebooted"):
        print("[deploy] Reboot and verify the updated host", flush=True)
        boot = remote.run("cat /proc/sys/kernel/random/boot_id", capture=True).stdout.strip()
        remote.run("sudo shutdown -r +0", check=False, capture=True)
        remote.wait(old_boot=boot)
        state["rebooted"] = True
        save()
    if not state.get("kernels_cleaned"):
        remote.upload(ROOT / "deploy/bootstrap.py", "retail-bootstrap.py")
        command = [
            "sudo",
            "python3",
            "/home/deploy/retail-bootstrap.py",
            "--owner",
            state["owner"],
            "--finish-after-reboot",
        ]
        for cidr in config["admin_cidrs"]:
            command.extend(["--admin-cidr", cidr])
        remote.run(shlex.join(command))
        state["kernels_cleaned"] = True
        save()
    remote.run("nvidia-smi\ndocker info --format '{{.ServerVersion}}'")
    if not state.get("installed"):
        print(
            "[deploy] Install the application, download models and build scanned images", flush=True
        )
        archive = folder / ("source-" + secrets.token_hex(4) + ".zip")
        export(archive)
        remote.upload(archive, "retail-source.zip")
        remote.run(
            "umask 077\nmkdir -p /home/deploy/retail-unpack\n"
            "python3 -m zipfile -e /home/deploy/retail-source.zip /home/deploy/retail-unpack\n"
            "test ! -e /home/deploy/retail-app || cmp -s /etc/retail-voice-owner /home/deploy/retail-app/.retail-owner\n"
            "mkdir -p /home/deploy/retail-app\n"
            "cp -a /home/deploy/retail-unpack/retail-voice-demo/. /home/deploy/retail-app/\n"
            "cp /etc/retail-voice-owner /home/deploy/retail-app/.retail-owner"
        )
        command = [
            "python3.12",
            "scripts/gpu.py",
            "configure",
            "--site",
            config["site"],
            "--region",
            config["region"],
        ]
        if args.reference:
            photo = Path(args.reference).expanduser().resolve()
            name = "retail-reference" + photo.suffix.lower()
            remote.upload(photo, name)
            remote.run("chmod 600 /home/deploy/" + name)
            command.extend(["--reference", "/home/deploy/" + name, "--tenant", args.tenant])
        elif args.demo_person:
            command.extend(["--demo-person", "--tenant", args.tenant])
        installation = ["python3.12", "scripts/gpu.py", "install", "--accept-model-licenses"]
        if args.accept_host_advisories:
            installation.append("--accept-host-advisories")
        state["unfixed_host_advisories_accepted_for_demo"] = args.accept_host_advisories
        save()
        remote.run(
            "cd /home/deploy/retail-app\npython3.12 scripts/demo.py setup\n"
            + shlex.join(command)
            + "\n"
            + shlex.join(installation)
            + "\nprintf '[deploy] Start services and wait for model compilation\\n'\n"
            "python3.12 scripts/gpu.py services\n"
            "sudo install -m 644 .local/systemd/retail-*.service /etc/systemd/system/\n"
            "sudo systemctl daemon-reload\n"
            "sudo systemctl enable --now retail-vlm retail-speech retail-gateway\n"
            "if test -f .local/systemd/retail-tryon.service; then sudo systemctl enable --now retail-tryon; fi\n"
            "/bin/sh deploy/wait-ready.sh all\n"
            "/bin/sh deploy/wait-ready.sh gateway\n"
            "if test -f .local/systemd/retail-tryon.service; then /bin/sh deploy/wait-ready.sh tryon; fi\n"
            "set -a\n. .local/gateway.env\nset +a\n.venv/bin/python -m retail_voice.cli preflight"
        )
        state["installed"] = True
        save()
    print("[deploy] Site installation is complete", flush=True)
    print(
        f"Site {config['site']} is installed at {address}. Keep {folder} private for resuming and access."
    )
    print("Create the browser tunnel from your computer:")
    print(shlex.join(["ssh", *remote.options, "-N", "-L", "8765:127.0.0.1:8765", remote.host]))
    print(
        "Open http://localhost:8765. Retrieve the retailer key privately from .local/gateway.env on the host."
    )


def deployment_clearance(verify_candidate):
    if verify_candidate:
        print(
            "Candidate verification only. Release clearance is unchanged; security scans remain required."
        )
    else:
        require_release_clearance()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply"])
    parser.add_argument("--site", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--gpu-type", default="g3-gpu-rtxpro6000-blackwell-1")
    parser.add_argument("--ssh-key", required=True)
    parser.add_argument("--admin-cidr", action="append", required=True)
    person = parser.add_mutually_exclusive_group()
    person.add_argument("--reference")
    person.add_argument(
        "--demo-person", action="store_true", help="Use the bundled fictional person"
    )
    parser.add_argument("--tenant", choices=["retailer-a", "retailer-b"], default="retailer-a")
    parser.add_argument("--accept-model-licenses", action="store_true")
    parser.add_argument(
        "--accept-host-advisories",
        action="store_true",
        help="Accept documented unfixed host findings for this restricted demo",
    )
    parser.add_argument(
        "--verify-candidate",
        action="store_true",
        help="Test an unpublished candidate; all dependency, host, and image gates still apply",
    )
    args = parser.parse_args()
    config = configuration(args)
    cloud = Cloud()
    plan = cloud.request("/linode/types/" + config["gpu_type"])
    availability = cloud.rows("/regions/" + config["region"] + "/availability")
    available = any(row["plan"] == config["gpu_type"] and row["available"] for row in availability)
    price = next(
        (row for row in plan.get("region_prices", []) if row["id"] == config["region"]),
        plan["price"],
    )
    print(
        json.dumps(
            {
                "site": config["site"],
                "region": config["region"],
                "gpu_type": config["gpu_type"],
                "price": price,
                "available": available,
                "ports": "22,80,443",
                "allowed_ipv4": config["admin_cidrs"],
                "new_resources": ["GPU host", "firewall"],
            },
            indent=2,
        )
    )
    if args.command == "plan":
        return
    deployment_clearance(args.verify_candidate)
    if not args.accept_model_licenses:
        raise ValueError("Apply requires --accept-model-licenses")
    init()
    folder = LOCAL / "sites" / config["site"]
    if (LOCAL / "sites").is_symlink() or folder.is_symlink():
        raise ValueError("Refusing a symlink for deployment state")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    descriptor = os.open(folder / "apply.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w"):
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        apply(cloud, config, args, folder, available=available)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
