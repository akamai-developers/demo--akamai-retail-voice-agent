"""Configure an existing GPU host, install models, and render reviewable systemd units."""

import argparse
import getpass
import grp
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import deployment_profile as profile
from demo import LOCAL, PYTHON, ROOT, ensure_pip, init, private_write, read_env, run, write_env
from images import build as build_image
from public_check import demo_reference
from security import dependency_audit, host_audit
from security_tools import ensure as ensure_security_tools


def safe_path(value):
    path = Path(value).expanduser().resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
        raise ValueError("GPU deployment paths must contain only letters, numbers, /, _, ., and -")
    return path


def configure(args):
    runtime_settings = profile.runtime_settings(
        args.model_profile, args.context_tokens, args.gpu_memory_utilization
    )
    safe_path(ROOT)
    init()
    for value in (args.site, args.region):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value) or any(
            bad in value for bad in ("replace", "example", "local-mock", "unconfigured")
        ):
            raise ValueError("Use an actual lowercase site and Linode region label")
    model_dir = safe_path(args.model_dir or LOCAL / "models")
    runtime = safe_path(LOCAL / "runtime")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    settings = dict(
        site_id=args.site,
        region=args.region,
        model_root=str(model_dir),
        runtime_dir=str(runtime),
        context_tokens=runtime_settings["context_tokens"],
        gpu_memory_utilization=runtime_settings["gpu_memory_utilization"],
        turn_timeout_seconds=120,
        request_timeout_seconds=90,
        speaker_index=0,
    )
    existing_config = LOCAL / "gpu.json"
    if existing_config.is_file():
        image = json.loads(existing_config.read_text()).get("vllm_image")
        if image:
            settings["vllm_image"] = image
    values = read_env()
    values.update(DEMO_MODE="nvidia", GPU_CONFIG=str(LOCAL / "gpu.json"))
    values.setdefault("TRYON_MODE", "disabled")
    if args.reference or args.demo_person:
        reference = safe_path(demo_reference(ROOT) if args.demo_person else args.reference)
        if not reference.is_file() or reference.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Use a readable reference image smaller than 2 MB")
        values.update(
            TRYON_MODE="live", TRYON_TENANT_ID=args.tenant, TRYON_REFERENCE_IMAGE=str(reference)
        )
    private_write(LOCAL / "gpu.json", json.dumps(settings, indent=2) + "\n")
    write_env(values)
    write_env(
        {
            "GPU_CONFIG": values["GPU_CONFIG"],
            "PYTHONPATH": str(ROOT / "src"),
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        },
        LOCAL / "speech.env",
    )
    write_env(
        {
            "TRYON_MODEL_DIR": str(model_dir / "tryon"),
            "PYTHONPATH": str(ROOT / "src"),
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        },
        LOCAL / "tryon.env",
    )
    print(
        "Configured private site files. Next: python3.12 scripts/gpu.py install --accept-model-licenses"
    )


def install(args):
    if not args.accept_model_licenses:
        raise ValueError("Review the model licenses, then pass --accept-model-licenses")
    if sys.version_info[:2] != (3, 12) or not sys.platform.startswith("linux"):
        raise ValueError("GPU installation requires Linux and Python 3.12")
    for command in ("nvidia-smi", "docker", "git", "cmake", "pkg-config", "curl"):
        if not shutil.which(command):
            raise ValueError(f"Install the prerequisite first: {command}")
    run("nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv")
    run("docker", "info", "--format", "{{.ServerVersion}}")
    env = os.environ | read_env()
    if env.get("DEMO_MODE") != "nvidia":
        raise ValueError("Run scripts/gpu.py configure first")
    settings = json.loads(Path(env["GPU_CONFIG"]).read_text())
    runtime = json.loads((ROOT / "src/retail_voice/profiles/nvidia-one-gpu.json").read_text())[
        "runtime"
    ]
    ensure_security_tools()
    dependency_audit()
    host_audit(accept_host_advisories=args.accept_host_advisories)
    names = ["speech"] + (["tryon"] if env.get("TRYON_MODE") == "live" else [])
    for name in names:
        folder = ROOT / f".{name}-venv"
        python = folder / "bin/python"
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(folder)
        ensure_pip(python)
        run(
            python,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            f"deploy/{name}-requirements.lock.txt",
        )
        if name == "speech":
            run(
                python,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                "nemo_toolkit[asr-only,common-only] @ git+https://github.com/NVIDIA-NeMo/Speech.git@"
                + runtime["nemo_commit"],
            )
            run(python, "scripts/speech_compat.py")
        run(python, "-m", "pip", "check")
        run(
            python,
            "-c",
            "import torch,torchaudio,sys; assert torch.__version__.split('+')[0]==sys.argv[1]; "
            "assert torchaudio.__version__.split('+')[0]==sys.argv[2]; assert torch.cuda.is_available()",
            runtime["torch"],
            runtime["torchaudio"],
        )
        freeze = subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True)
        private_write(LOCAL / f"{name}-resolved.txt", freeze)
    run(PYTHON, "-m", "pip", "install", "-r", "deploy/download-requirements.txt")
    run(PYTHON, "-m", "retail_voice.cli", "download-models", "--accept-model-licenses", env=env)
    if "tryon" in names:
        run(
            PYTHON,
            "tools/download_tryon.py",
            "--output",
            str(Path(settings["model_root"]) / "tryon"),
            "--accept-model-licenses",
            env=env,
        )
    settings["vllm_image"] = build_image("vlm")
    private_write(Path(env["GPU_CONFIG"]), json.dumps(settings, indent=2) + "\n")
    print("Installed. Render services with: python3.12 scripts/gpu.py services")


def services(args):
    safe_path(ROOT)
    values = read_env()
    if values.get("DEMO_MODE") != "nvidia":
        raise ValueError("Run scripts/gpu.py configure first")
    user, group = getpass.getuser(), grp.getgrgid(os.getgid()).gr_name
    if not all(re.fullmatch(r"[a-z_][a-z0-9_-]*", value) for value in (user, group)):
        raise ValueError("Unsupported service user or group")
    if os.getuid() == 0:
        raise ValueError("Render as the non-root account that will own the services")
    directory = LOCAL / "systemd"
    directory.mkdir(exist_ok=True, mode=0o700)
    common = (
        f"User={user}\nGroup={group}\nWorkingDirectory={ROOT}\n"
        "UMask=0077\nRestart=on-failure\nRestartSec=15\nTimeoutStartSec=2100\nTimeoutStopSec=60\n"
    )
    specs = {
        "vlm": (
            "docker.service network-online.target",
            "gateway.env",
            f"ExecStart={PYTHON} -m retail_voice.cli serve-vlm --start\n"
            "ExecStop=/usr/bin/docker stop -t 30 retail-vlm\n",
        ),
        "speech": (
            "retail-vlm.service",
            "speech.env",
            f"ExecStartPre=/bin/sh {ROOT}/deploy/wait-ready.sh vlm\n"
            f"ExecStart={ROOT}/.speech-venv/bin/python -m uvicorn retail_voice.speech_service:create_app --factory --host 127.0.0.1 --port 8082 --no-access-log\n",
        ),
        "gateway": (
            "retail-speech.service",
            "gateway.env",
            f"ExecStartPre=/bin/sh {ROOT}/deploy/wait-ready.sh all\n"
            f"ExecStart={PYTHON} -m uvicorn retail_voice.api:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log\n",
        ),
    }
    if values.get("TRYON_MODE") == "live":
        specs["tryon"] = (
            "retail-speech.service",
            "tryon.env",
            f"ExecStartPre=/bin/sh {ROOT}/deploy/wait-ready.sh all\n"
            f"ExecStart={ROOT}/.tryon-venv/bin/python -m uvicorn retail_voice.tryon_service:create_app --factory --host 127.0.0.1 --port 8083 --no-access-log\n",
        )
    if args.domain:
        if (
            not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain)
            or "." not in args.domain
        ):
            raise ValueError("Use a DNS hostname for --domain")
        supplied_tls = bool(args.tls_cert or args.tls_key)
        if supplied_tls and not (args.tls_cert and args.tls_key):
            raise ValueError("Supply both --tls-cert and --tls-key")
        if not supplied_tls and (
            not args.email or not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", args.email)
        ):
            raise ValueError("Supply a valid certificate contact with --email")
        template = (ROOT / "deploy/Caddyfile").read_text()
        mounts = ""
        if supplied_tls:
            certificate, key = safe_path(args.tls_cert), safe_path(args.tls_key)
            if not certificate.is_file() or not key.is_file():
                raise ValueError("TLS certificate and key must exist")
            if key.stat().st_mode & 0o077:
                raise ValueError("TLS private key must have owner-only permissions")
            template = template.replace("    email {$ACME_EMAIL}\n", "")
            template = template.replace(
                "{$DEMO_DOMAIN} {",
                "{$DEMO_DOMAIN} {\n    tls /certs/fullchain.pem /certs/private.key",
            )
            mounts = (
                f"--volume {certificate}:/certs/fullchain.pem:ro "
                f"--volume {key}:/certs/private.key:ro "
            )
        image_receipt = LOCAL / "caddy-image.json"
        if not image_receipt.is_file():
            raise ValueError("Build and scan the proxy first: python3 scripts/images.py caddy")
        receipt = json.loads(image_receipt.read_text())
        image = receipt.get("image", "")
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
            or receipt.get("vulnerability_scan") != "passed"
        ):
            raise ValueError("Proxy requires an audited immutable image")
        private_write(
            LOCAL / "Caddyfile",
            template.replace("{$DEMO_DOMAIN}", args.domain).replace(
                "{$ACME_EMAIL}", args.email or ""
            ),
        )
        for name in ("caddy-data", "caddy-config"):
            (LOCAL / name).mkdir(exist_ok=True, mode=0o700)
        specs["caddy"] = (
            "docker.service retail-gateway.service",
            "speech.env",
            f"ExecStart=/usr/bin/docker run --rm --name retail-caddy --network host --user {os.getuid()}:{os.getgid()} "
            "--read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m "
            "--cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges "
            f"--volume {LOCAL}/Caddyfile:/etc/caddy/Caddyfile:ro "
            f"--volume {LOCAL}/caddy-data:/data --volume {LOCAL}/caddy-config:/config "
            f"{mounts}{image}\nExecStop=/usr/bin/docker stop -t 10 retail-caddy\n",
        )
    for name, (after, env_file, commands) in specs.items():
        hardening = (
            "NoNewPrivileges=yes\nLimitCORE=0\n"
            "InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock\n"
            if name in {"gateway", "speech", "tryon"}
            else ""
        )
        unit = (
            f"[Unit]\nDescription=Retail voice {name}\nAfter={after}\nWants={after}\n\n"
            f"[Service]\n{common}{hardening}EnvironmentFile={LOCAL / env_file}\n{commands}\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        private_write(directory / f"retail-{name}.service", unit)
    print("Review .local/systemd/, then install the units using the commands in docs/deploy.md.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure")
    config.add_argument("--site", default="gpu-1")
    config.add_argument("--region", required=True)
    config.add_argument("--model-dir")
    profile.add_arguments(config)
    person = config.add_mutually_exclusive_group()
    person.add_argument("--reference")
    person.add_argument(
        "--demo-person", action="store_true", help="Use the bundled fictional person"
    )
    config.add_argument("--tenant", choices=["retailer-a", "retailer-b"], default="retailer-a")
    setup = commands.add_parser("install")
    setup.add_argument("--accept-model-licenses", action="store_true")
    setup.add_argument(
        "--accept-host-advisories",
        action="store_true",
        help="Accept documented unfixed host findings for a restricted demo",
    )
    render = commands.add_parser("services")
    render.add_argument("--domain")
    render.add_argument("--email")
    render.add_argument("--tls-cert")
    render.add_argument("--tls-key")
    args = parser.parse_args()
    {"configure": configure, "install": install, "services": services}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
