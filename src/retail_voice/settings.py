"""One-host configuration. Model endpoints cannot be supplied by a shopper."""

import hashlib
import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


def model_manifest():
    return json.loads((Path(__file__).parent / "profiles/nvidia-one-gpu.json").read_text())


class GPUSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_id: str
    region: str
    model_root: Path
    runtime_dir: Path
    turn_timeout_seconds: int = Field(default=120, ge=10, le=180)
    request_timeout_seconds: int = Field(default=90, ge=1, le=120)
    speaker_index: int = Field(default=0, ge=0, le=4)
    gpu_memory_utilization: float = Field(default=0.7, ge=0.4, le=0.85)
    # vLLM --max-model-len. Part of the profile digest through model_dump, so a change is a
    # new profile that the speech service and gateway must both report.
    context_tokens: int = Field(default=4096, ge=2048, le=32768)

    @field_validator("site_id", "region")
    @classmethod
    def labels(cls, value):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value) or any(
            x in value for x in ("replace", "example", "local-mock", "unconfigured")
        ):
            raise ValueError("Set an actual site/region label")
        return value

    @field_validator("model_root", "runtime_dir")
    @classmethod
    def absolute_paths(cls, value):
        if not value.is_absolute() or "absolute/path" in str(value):
            raise ValueError("Set an actual absolute path")
        return value

    @classmethod
    def from_env(cls):
        path = os.getenv("GPU_CONFIG")
        if not path:
            raise RuntimeError("NVIDIA mode requires GPU_CONFIG pointing to your site JSON")
        return cls.model_validate_json(Path(path).read_text())

    @property
    def quarantine_path(self):
        return self.runtime_dir / "backend-quarantine.json"

    @property
    def profile_digest(self):
        body = json.dumps(
            {"settings": self.model_dump(mode="json"), "manifest": model_manifest()}, sort_keys=True
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def artifact(self, name):
        model = model_manifest()["models"][name]
        root = self.model_root / name
        return root / model["file"] if "file" in model else root
