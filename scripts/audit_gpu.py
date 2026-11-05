"""Audit direct GPU pins without downloading weights or importing CUDA libraries."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = json.loads((ROOT / "src/retail_voice/profiles/nvidia-one-gpu.json").read_text())
    pins = {name: manifest["runtime"][name] for name in ("torch", "torchaudio")}
    for name in ("speech", "tryon", "download"):
        for line in (ROOT / f"deploy/{name}-requirements.txt").read_text().splitlines():
            if "==" in line and not line.startswith("#"):
                package, version = line.strip().split("==", 1)
                if package in pins and pins[package] != version:
                    raise SystemExit(f"Conflicting GPU pin: {package}")
                pins[package] = version
    print(
        "Coverage: direct PyPI pins only. Audit each resolved GPU environment separately.",
        flush=True,
    )
    print("NeMo source and container OS packages require separate review.", flush=True)
    with tempfile.TemporaryDirectory() as directory:
        requirements = Path(directory) / "requirements.txt"
        requirements.write_text(
            "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))
        )
        command = ["pip-audit", "--disable-pip", "--no-deps", "-r", str(requirements)]
        if args.output:
            command.extend(["--format", "json", "--output", str(args.output.resolve())])
        raise SystemExit(subprocess.run(command, check=False).returncode)


if __name__ == "__main__":
    main()
