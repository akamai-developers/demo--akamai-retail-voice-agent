"""Apply two reviewed inference compatibility changes to exact vendor source files."""

import hashlib
import importlib.util
from pathlib import Path

PATCHES = (
    (
        "nv_one_logger",
        "training_telemetry/integration/pytorch_lightning.py",
        "70d04b515b014b030cefb7ccd494326543d13291ff36da29b60a4ed36c142069",
        "ff8fa1bbc8e317c4ebf573a48cb90b7bcd289ee7babc9d8430c7a506fcab0262",
        (
            (
                "weights_only: bool = False, storage_options:",
                "weights_only: Optional[bool] = False, storage_options:",
            ),
        ),
    ),
    (
        "nemo",
        "utils/exp_manager.py",
        "5959c8f9b704eba59124b0d9fb761537a268028287c60ad94880df2584fcd5a9",
        "ffd526d0c9dab2518aeb98d1da9d3cfd99e96e94d411be8c4c060fb947f9cf48",
        (
            (
                "from lightning.pytorch.loggers import MLFlowLogger, NeptuneLogger, TensorBoardLogger, WandbLogger",
                "from lightning.pytorch.loggers import MLFlowLogger, TensorBoardLogger, WandbLogger",
            ),
            (
                "        neptune_logger = NeptuneLogger(**neptune_kwargs)",
                "        from lightning.pytorch.loggers import NeptuneLogger\n\n"
                "        neptune_logger = NeptuneLogger(**neptune_kwargs)",
            ),
        ),
    ),
)


def transform(data, before, after, changes):
    current = hashlib.sha256(data).hexdigest()
    if current == after:
        return data
    if current != before:
        raise ValueError("Vendor source changed; review compatibility before installation")
    text = data.decode()
    for old, new in changes:
        if text.count(old) != 1:
            raise ValueError("Expected one compatibility edit")
        text = text.replace(old, new)
    result = text.encode()
    if hashlib.sha256(result).hexdigest() != after:
        raise ValueError("Compatibility result does not match the reviewed source")
    return result


def main():
    for package, relative, before, after, changes in PATCHES:
        spec = importlib.util.find_spec(package)
        if not spec or not spec.submodule_search_locations:
            raise ValueError(f"Install the pinned {package} package first")
        folder = Path(next(iter(spec.submodule_search_locations)))
        path = folder / relative
        if path.is_symlink():
            raise ValueError("Vendor source must not be a symlink")
        original = path.read_bytes()
        updated = transform(original, before, after, changes)
        if updated != original:
            path.write_bytes(updated)
        print(f"Verified inference compatibility: {package}/{relative}")


if __name__ == "__main__":
    main()
