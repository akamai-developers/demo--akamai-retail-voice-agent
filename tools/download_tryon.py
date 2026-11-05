"""Explicit operator download of the pinned optional image model. Does not start inference."""

import argparse
import json
from pathlib import Path

from retail_voice.tryon import MODEL_ID, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--accept-model-licenses", action="store_true")
    args = parser.parse_args()
    if not args.accept_model_licenses:
        parser.error("Review the model license, then pass --accept-model-licenses")
    if not args.output.is_absolute():
        parser.error("--output must be an absolute private model directory")
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(args.output),
        allow_patterns=[
            "model_index.json",
            "scheduler/*",
            "tokenizer/*",
            "text_encoder/*",
            "transformer/*",
            "vae/*",
            "LICENSE.md",
        ],
    )
    (args.output / "tryon-model.json").write_text(
        json.dumps({"model_id": MODEL_ID, "revision": MODEL_REVISION}) + "\n"
    )
    print("Pinned try-on snapshot downloaded. Hardware validation is still required.")


if __name__ == "__main__":
    main()
