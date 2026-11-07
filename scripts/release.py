"""Export allowlisted source files without git history or local runtime state."""

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path

from public_check import ROOT, check
from security import require_release_clearance


def export(destination, publish=False):
    if publish:
        require_release_clearance()
    paths = check(publish=publish)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Destination already exists; choose a new filename")
    manifest = []
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            manifest.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
            info = zipfile.ZipInfo("retail-voice-demo/" + relative)
            mode = 0o755 if path.suffix == ".sh" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        archive.writestr(
            "retail-voice-demo/release-manifest.json", json.dumps(manifest, indent=2) + "\n"
        )
    print(f"Exported {len(paths)} files to {destination.name}")
    print("Scan the extracted archive with Gitleaks before sharing it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    export(args.output, args.publish)


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
