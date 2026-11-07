"""Build a wheel and run its asset smoke test in a fresh environment outside src."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    artifact_dir = ROOT / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=artifact_dir) as directory:
        work = Path(directory).resolve()
        # Keep pip's temporary build files in the disposable workspace, even when /tmp is restricted.
        temp = work / "tmp"
        temp.mkdir()
        env = os.environ | {"TMPDIR": str(temp)}
        wheels = work / "wheels"
        source = work / "source"
        shutil.copytree(
            ROOT,
            source,
            ignore=shutil.ignore_patterns(
                "artifacts",
                "build",
                "dist",
                ".venv",
                ".speech-venv",
                ".tryon-venv",
                ".security-venv",
                ".local",
                ".git",
                "requirements-listed",
                "private-demo",
                "gpu.local.json",
                "__pycache__",
                "*.egg-info",
                ".mypy_cache",
                ".ruff_cache",
                ".env",
                "build-logs",
            ),
        )
        run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            str(source),
            "-w",
            str(wheels),
            env=env,
        )
        run(sys.executable, "-m", "venv", str(work / "venv"))
        python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            str(ROOT / "requirements.lock.txt"),
            env=env,
        )
        run(
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(next(wheels.glob("*.whl"))),
            env=env,
        )
        env.pop("PYTHONPATH", None)
        run(str(python), str(ROOT / "tests/package_check.py"), cwd=work, env=env)


if __name__ == "__main__":
    main()
