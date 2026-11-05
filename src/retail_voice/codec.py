"""Prepare a private inference codec without loading training-only speech models."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_codec(source, directory, codec_type, source_revision):
    """Keep the pinned source intact and derive an offline inference checkpoint once."""
    source, directory = Path(source), Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = directory / "inference-codec.nemo"
    receipt = directory / "inference-codec.json"
    identity = {
        "schema_version": 1,
        "source_sha256": file_hash(source),
        "source_revision": source_revision,
    }
    try:
        previous = json.loads(receipt.read_text())
        if all(previous.get(key) == value for key, value in identity.items()) and file_hash(
            output
        ) == previous.get("output_sha256"):
            return output
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    config = codec_type.restore_from(str(source), return_config=True)
    # Magpie discards the discriminator after restoration. Excluding it before restoration
    # avoids fetching its training model on a clean host with offline inference enabled.
    config["discriminator"] = None
    for name in ("use_scl_loss", "use_slm_loss"):
        if name in config:
            config[name] = False
    codec = codec_type.restore_from(
        str(source), override_config_path=config, strict=False, map_location="cpu"
    )
    with tempfile.TemporaryDirectory(prefix="codec-", dir=directory) as temporary:
        checkpoint = Path(temporary) / "inference.nemo"
        codec.save_to(str(checkpoint))
        checkpoint.chmod(0o600)
        identity["output_sha256"] = file_hash(checkpoint)
        metadata = Path(temporary) / "receipt.json"
        metadata.write_text(json.dumps(identity, indent=2) + "\n")
        metadata.chmod(0o600)
        os.replace(checkpoint, output)
        os.replace(metadata, receipt)
    return output
