"""Expose versioned CUDA wheel libraries to the native JIT linker."""

import re
from pathlib import Path

root = Path("/opt/runtime/lib/python3.11/site-packages/nvidia/cu13")
library = root / "lib"
groups = {}
for path in library.glob("*.so.*"):
    match = re.fullmatch(r"(.+\.so)\.[0-9]+(?:\.[0-9]+)*", path.name)
    if match and path.is_file():
        groups.setdefault(match[1], set()).add(path.resolve())
for name, targets in groups.items():
    target = library / name
    if target.exists():
        continue
    if len(targets) != 1:
        raise RuntimeError(f"Ambiguous CUDA library versions: {name}")
    target.symlink_to(next(iter(targets)).name)
if not (root / "lib64").exists():
    (root / "lib64").symlink_to("lib", target_is_directory=True)
for name in ("libcudart.so", "libcublas.so", "libcublasLt.so", "libnvrtc.so"):
    if not (library / name).is_file():
        raise RuntimeError(f"Required CUDA library missing: {name}")
