"""Install and run the demo locally. Credentials stay in .local/gateway.env."""

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
ENV = LOCAL / "gateway.env"
PYTHON = ROOT / ".venv/bin/python"


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT, **kwargs)


def private_write(path, text):
    """Create or replace a private file without following symlinks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path.name}")
    temporary = path.with_name(path.name + ".new-" + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_env(path=ENV):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value)
        if len(parts) != 1:
            raise ValueError("Invalid environment file")
        values[key] = parts[0]
    return values


def write_env(values, path=ENV):
    if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
        raise ValueError("Environment values must fit on one line")
    private_write(path, "".join(f"{k}={json.dumps(str(v))}\n" for k, v in values.items()))


def init():
    if LOCAL.is_symlink():
        raise ValueError("Refusing a symlink for .local")
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    LOCAL.chmod(0o700)
    if ENV.is_symlink():
        raise ValueError("Refusing a symlink for gateway.env")
    if ENV.exists():
        values = read_env()
        keys = [values.get(k, "") for k in ("TENANT_A_KEY", "TENANT_B_KEY", "OPERATOR_KEY")]
        if len(set(keys)) != 3 or any(len(k) < 32 for k in keys):
            raise ValueError("Existing credentials are invalid; restore or rotate them explicitly")
        ENV.chmod(0o600)
    else:
        values = {
            k: secrets.token_urlsafe(32) for k in ("TENANT_A_KEY", "TENANT_B_KEY", "OPERATOR_KEY")
        }
        write_env({"DEMO_MODE": "mock", "TRYON_MODE": "disabled", **values})


def setup():
    if sys.version_info[:2] != (3, 12) or os.name != "posix":
        raise ValueError("Use Python 3.12 on macOS or Linux.")
    init()
    if not PYTHON.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    run(PYTHON, "-m", "pip", "install", "--require-hashes", "-r", "requirements.lock.txt")
    run(PYTHON, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", ".")
    run(PYTHON, "-m", "pip", "check")
    print("Installed. Start with: python3 scripts/demo.py run")
    print("Open .local/gateway.env privately and use TENANT_A_KEY to sign in.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "init", "run", "rotate-keys"])
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    init()
    if args.command == "rotate-keys":
        values = read_env()
        values.update(
            {k: secrets.token_urlsafe(32) for k in ("TENANT_A_KEY", "TENANT_B_KEY", "OPERATOR_KEY")}
        )
        write_env(values)
        print("Keys rotated. Restart the gateway; existing sessions end on restart.")
    elif args.command == "run":
        if not PYTHON.exists():
            raise ValueError("Run scripts/demo.py setup first")
        os.chdir(ROOT)
        os.execve(
            str(PYTHON),
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "retail_voice.api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--no-access-log",
            ],
            os.environ | read_env(),
        )
    else:
        print("Private configuration is ready in .local/gateway.env")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
