"""Prepare only a new host carrying the ownership marker written by site.py."""

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import subprocess
import urllib.request
from functools import cmp_to_key
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True, env=os.environ | {"DEBIAN_FRONTEND": "noninteractive"})


def download(url, target, expected):
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(10 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Bootstrap signing-key checksum mismatch")
    target.write_bytes(data)
    target.chmod(0o644)


def obsolete_kernels(inventory, running_kernel):
    pattern = r"linux-(?:image(?:-unsigned)?|modules(?:-extra)?|headers|tools)-(\d+\.\d+\.\d+-\d+)(?:-generic)?"
    installed = {}
    images = set()
    for line in inventory.splitlines():
        name, status = line.split("\t")
        match = re.fullmatch(pattern, name)
        if status != "installed" or not match:
            continue
        installed[name] = match[1]
        if name.startswith("linux-image-") and name.endswith("-generic"):
            images.add(match[1])
    running = running_kernel.removesuffix("-generic")
    if running not in images:
        raise ValueError("Running kernel is not an installed generic image; refusing cleanup")

    def compare(left, right):
        return (
            tuple(map(int, re.split(r"[.-]", left))) > tuple(map(int, re.split(r"[.-]", right)))
        ) - (tuple(map(int, re.split(r"[.-]", left))) < tuple(map(int, re.split(r"[.-]", right))))

    newest = sorted(images, key=cmp_to_key(compare), reverse=True)[:2]
    keep = set(newest) | {running}
    floor = min(keep, key=cmp_to_key(compare))
    return sorted(name for name, version in installed.items() if compare(version, floor) < 0)


def finish_after_reboot():
    inventory = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n", "linux-*"], text=True
    )
    packages = obsolete_kernels(inventory, platform.release())
    if packages:
        preview = subprocess.check_output(["apt-get", "-s", "purge", *packages], text=True)
        removals = {
            line.split()[1] for line in preview.splitlines() if line.startswith(("Remv ", "Purg "))
        }
        if not removals or not removals.issubset(set(packages)):
            raise ValueError(
                "Kernel cleanup would affect other packages; inspect before continuing"
            )
        run("apt-get", "purge", "-y", *packages)
    print(
        json.dumps(
            {"removed_obsolete_kernel_packages": packages, "running_kernel": platform.release()}
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--finish-after-reboot", action="store_true")
    parser.add_argument("--admin-cidr", action="append", required=True)
    args = parser.parse_args()
    if os.getuid() != 0 or not re.fullmatch(r"[a-f0-9]{32}", args.owner):
        raise ValueError("Run through site.py on its newly created host")
    marker = Path("/etc/retail-voice-owner")
    if not marker.is_file() or marker.read_text().strip() != args.owner:
        raise ValueError("This host does not belong to this deployment")
    addresses = [ipaddress.ip_network(value, strict=True) for value in args.admin_cidr]
    if any(network.version != 4 or network.prefixlen != 32 for network in addresses):
        raise ValueError("Administrator addresses must be individual IPv4 /32 entries")
    release = Path("/etc/os-release").read_text()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("Bootstrap requires Ubuntu 24.04")
    if args.finish_after_reboot:
        finish_after_reboot()
        return
    run("cloud-init", "status", "--wait")
    run("apt-get", "update")
    run("apt-get", "upgrade", "--with-new-pkgs", "-y")
    run(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "ca-certificates",
        "curl",
        "gnupg",
        "python3.12-venv",
        "python3.12-dev",
        "build-essential",
        "cmake",
        "pkg-config",
        "git",
        "libsndfile1",
        "ffmpeg",
        "ufw",
        "linux-generic-hwe-24.04",
        "linux-headers-generic-hwe-24.04",
        "pciutils",
    )
    run("ufw", "default", "deny", "incoming")
    run("ufw", "default", "allow", "outgoing")
    for address in addresses:
        for port in ("22", "80", "443"):
            run("ufw", "allow", "from", str(address), "to", "any", "port", port, "proto", "tcp")
    run("ufw", "--force", "enable")
    folder = Path("/var/lib/retail-bootstrap")
    folder.mkdir(mode=0o700, exist_ok=True)
    download(
        "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb",
        folder / "cuda-keyring.deb",
        "d2a6b11c096396d868758b86dab1823b25e14d70333f1dfa74da5ddaf6a06dba",
    )
    run("dpkg", "-i", str(folder / "cuda-keyring.deb"))
    keys = Path("/etc/apt/keyrings")
    keys.mkdir(mode=0o755, exist_ok=True)
    download(
        "https://download.docker.com/linux/ubuntu/gpg",
        keys / "retail-docker.asc",
        "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
    )
    download(
        "https://nvidia.github.io/libnvidia-container/gpgkey",
        keys / "retail-nvidia.asc",
        "c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9",
    )
    sources = Path("/etc/apt/sources.list.d")
    (sources / "retail-docker.list").write_text(
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/retail-docker.asc] https://download.docker.com/linux/ubuntu noble stable\n"
    )
    (sources / "retail-nvidia.list").write_text(
        "deb [signed-by=/etc/apt/keyrings/retail-nvidia.asc] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /\n"
    )
    run("apt-get", "update")
    run(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "nvidia-open",
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "nvidia-container-toolkit",
    )
    # These server protocols are unused by the dedicated inference host.
    disabled_modules = ("ksmbd", "nvmet", "tipc")
    loaded_modules = {line.split()[0] for line in Path("/proc/modules").read_text().splitlines()}
    if loaded_modules.intersection(disabled_modules):
        raise ValueError("An unused server module is loaded; inspect the host before continuing")
    Path("/etc/modprobe.d/retail-host-security.conf").write_text(
        "# Disable autoload of unused SMB, NVMe target, and TIPC services.\n"
        + "".join(
            f"blacklist {module}\ninstall {module} /bin/false\n" for module in disabled_modules
        )
    )
    run("update-initramfs", "-u", "-k", "all")
    run("nvidia-ctk", "runtime", "configure", "--runtime=docker")
    run("systemctl", "restart", "docker")
    run("usermod", "-aG", "docker", "deploy")
    packages = subprocess.check_output(
        ["dpkg-query", "-W", "nvidia-open", "nvidia-container-toolkit", "docker-ce"], text=True
    )
    (folder / "receipt.json").write_text(
        json.dumps({"owner": args.owner, "packages": packages}, indent=2) + "\n"
    )
    print("Host prerequisites installed. Reboot before starting the GPU runtime.")


if __name__ == "__main__":
    main()
