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

from demo import LOCAL, PYTHON, ROOT, init, private_write, read_env, run, write_env


def safe_path(value):
    path = Path(value).expanduser().resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
        raise ValueError("GPU deployment paths must contain only letters, numbers, /, _, ., and -")
    return path


def configure(args):
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
        context_tokens=16384,
        gpu_memory_utilization=0.7,
        turn_timeout_seconds=120,
        request_timeout_seconds=90,
        speaker_index=0,
    )
    values = read_env()
    values.update(DEMO_MODE="nvidia", GPU_CONFIG=str(LOCAL / "gpu.json"))
    if args.reference:
        reference = safe_path(args.reference)
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
    cuda = runtime["cuda_wheels"]
    if not re.fullmatch(r"cu[0-9]+", cuda):
        raise ValueError("Unsupported CUDA wheel index in the model profile")
    constraints = LOCAL / "gpu-constraints.txt"
    private_write(
        constraints, "".join(f"{name}=={runtime[name]}\n" for name in ("torch", "torchaudio"))
    )
    names = ["speech"] + (["tryon"] if env.get("TRYON_MODE") == "live" else [])
    for name in names:
        folder = ROOT / f".{name}-venv"
        python = folder / "bin/python"
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(folder)
        run(
            python,
            "-m",
            "pip",
            "install",
            f"torch=={runtime['torch']}",
            f"torchaudio=={runtime['torchaudio']}",
            "--index-url",
            f"https://download.pytorch.org/whl/{cuda}",
        )
        run(
            python,
            "-m",
            "pip",
            "install",
            "-c",
            constraints,
            "-r",
            f"deploy/{name}-requirements.txt",
        )
        if name == "tryon":
            # The worker shares gateway input validation and needs its Python dependencies.
            run(python, "-m", "pip", "install", "--require-hashes", "-r", "requirements.lock.txt")
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
        "UMask=0077\nRestart=on-failure\nRestartSec=15\nTimeoutStartSec=1200\nTimeoutStopSec=60\n"
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
            f"ExecStartPre={ROOT}/deploy/wait-ready.sh vlm\n"
            f"ExecStart={ROOT}/.speech-venv/bin/python -m uvicorn retail_voice.speech_service:create_app --factory --host 127.0.0.1 --port 8082 --no-access-log\n",
        ),
        "gateway": (
            "retail-speech.service",
            "gateway.env",
            f"ExecStartPre={ROOT}/deploy/wait-ready.sh all\n"
            f"ExecStart={PYTHON} -m uvicorn retail_voice.api:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log\n",
        ),
    }
    if values.get("TRYON_MODE") == "live":
        specs["tryon"] = (
            "retail-speech.service",
            "tryon.env",
            f"ExecStartPre={ROOT}/deploy/wait-ready.sh all\n"
            f"ExecStart={ROOT}/.tryon-venv/bin/python -m uvicorn retail_voice.tryon_service:create_app --factory --host 127.0.0.1 --port 8083 --no-access-log\n",
        )
    if args.domain:
        if (
            not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain)
            or "." not in args.domain
        ):
            raise ValueError("Use a DNS hostname for --domain")
        if not args.email or not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", args.email):
            raise ValueError("Supply a valid certificate contact with --email")
        template = (ROOT / "deploy/Caddyfile").read_text()
        private_write(
            LOCAL / "Caddyfile",
            template.replace("{$DEMO_DOMAIN}", args.domain).replace("{$ACME_EMAIL}", args.email),
        )
        for name in ("caddy-data", "caddy-config"):
            (LOCAL / name).mkdir(exist_ok=True, mode=0o700)
        specs["caddy"] = (
            "docker.service retail-gateway.service",
            "speech.env",
            f"ExecStart=/usr/bin/docker run --rm --name retail-caddy --network host "
            f"--volume {LOCAL}/Caddyfile:/etc/caddy/Caddyfile:ro "
            f"--volume {LOCAL}/caddy-data:/data --volume {LOCAL}/caddy-config:/config "
            "retail-caddy:2.10.2-ratelimit\nExecStop=/usr/bin/docker stop -t 10 retail-caddy\n",
        )
    for name, (after, env_file, commands) in specs.items():
        unit = (
            f"[Unit]\nDescription=Retail voice {name}\nAfter={after}\nWants={after}\n\n"
            f"[Service]\n{common}EnvironmentFile={LOCAL / env_file}\n{commands}\n"
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
    config.add_argument("--reference")
    config.add_argument("--tenant", choices=["retailer-a", "retailer-b"], default="retailer-a")
    setup = commands.add_parser("install")
    setup.add_argument("--accept-model-licenses", action="store_true")
    render = commands.add_parser("services")
    render.add_argument("--domain")
    render.add_argument("--email")
    args = parser.parse_args()
    {"configure": configure, "install": install, "services": services}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
