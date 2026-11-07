"""Build and audit local runtime images before recording their immutable IDs."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from demo import LOCAL, ROOT, private_write, run
from security import container_audit


def build(name):
    files = [ROOT / "deploy" / name / "Dockerfile"]
    if name == "vlm":
        files.append(ROOT / "deploy/vlm-requirements.lock.txt")
        files.append(ROOT / "deploy/vlm/link_cuda.py")
    recipe = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()
    previous = LOCAL / f"{name}-image.json"
    image = None
    if previous.is_file():
        saved = json.loads(previous.read_text())
        candidate = saved.get("image", "")
        if saved.get("recipe_sha256") == recipe and re.fullmatch(r"sha256:[0-9a-f]{64}", candidate):
            found = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", candidate],
                capture_output=True,
                text=True,
            )
            if found.returncode == 0 and found.stdout.strip() == candidate:
                image = candidate
    # Re-scan an unchanged image when resuming; rebuilding creates a new attestation ID
    # and invalidates an otherwise reusable GPU compiler cache.
    if image is None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for path in files:
                shutil.copyfile(path, directory / path.name)
            image_id = directory / "image-id"
            run(
                "docker", "build", "--iidfile", image_id, "--tag", f"retail-{name}:local", directory
            )
            image = image_id.read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Build did not return an immutable image ID")
    required_types = {"wolfi", "python-pkg"} if name == "vlm" else {"gobinary"}
    container_audit(image, LOCAL / "security" / f"{name}-container.json", required_types)
    receipt = {
        "image": image,
        "recipe_sha256": recipe,
        "created_utc": datetime.now(UTC).isoformat(),
        "vulnerability_scan": "passed",
    }
    private_write(LOCAL / f"{name}-image.json", json.dumps(receipt, indent=2) + "\n")
    print(f"Built and audited {name}: {image}")
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=["vlm", "caddy"])
    args = parser.parse_args()
    build(args.runtime)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
