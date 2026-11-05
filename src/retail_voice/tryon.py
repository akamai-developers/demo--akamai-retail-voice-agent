"""Private, tenant-bound try-on previews. References and prepared assets stay outside git.

Live mode calls only the local image worker through the NVIDIA inference/quarantine boundary.
Prepared mode is explicitly labeled and never represented as live inference.
"""

import io
import json
import os
import re
from pathlib import Path

import httpx
from PIL import Image

from .media import MAX_IMAGE, sanitize_image

REQUEST = re.compile(
    r"\b(?:see|show|picture|preview).{0,70}\b(?:on me|me wearing)|"
    r"\b(?:try (?:it|this|that|them|the .{1,50}) on|try on|virtual try.on)\b",
    re.I,
)
NEGATIVE = re.compile(r"\b(?:don't|don’t|do not|never)\b.{0,50}(?:see|show|try|preview)", re.I)
MODEL_ID = "black-forest-labs/FLUX.2-klein-4B"
MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"


def requested(text):
    return bool(REQUEST.search(text) and not NEGATIVE.search(text))


def jpeg(data):
    clean = sanitize_image(data)
    with Image.open(io.BytesIO(clean)) as image:
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)
        return out.getvalue()


class TryOn:
    def __init__(self, catalog, backend, mode=None, tenant=None, reference=None, directory=None):
        self.catalog, self.backend = catalog, backend
        self.mode = mode or os.getenv("TRYON_MODE", "disabled")
        self.tenant = tenant or os.getenv("TRYON_TENANT_ID", "retailer-a")
        self.reference = None
        self.prepared: dict[tuple[str, str], bytes] = {}
        if self.mode not in {"disabled", "prepared", "live"}:
            raise ValueError("invalid_tryon_mode")
        if self.tenant not in {"retailer-a", "retailer-b"}:
            raise ValueError("invalid_tryon_tenant")
        if self.mode == "live":
            if backend.mode != "nvidia":
                raise ValueError("live_tryon_requires_nvidia")
            path = Path(reference or os.environ["TRYON_REFERENCE_IMAGE"]).resolve(strict=True)
            if path.stat().st_size > MAX_IMAGE:
                raise ValueError("tryon_reference_too_large")
            self.reference = jpeg(path.read_bytes())
        elif self.mode == "prepared":
            root = Path(directory or os.environ["TRYON_DIR"]).resolve(strict=True)
            manifest = root / "previews.json"
            if manifest.stat().st_size > 65536:
                raise ValueError("tryon_manifest_too_large")
            entries = json.loads(manifest.read_text())
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError("invalid_tryon_manifest")
            for entry in entries:
                if set(entry) != {"product_id", "color", "file"}:
                    raise ValueError("invalid_tryon_entry")
                card = catalog.card(self.tenant, entry["product_id"])
                if entry["color"] not in card["colors"]:
                    raise ValueError("invalid_tryon_color")
                path = (root / entry["file"]).resolve(strict=True)
                if not path.is_relative_to(root) or path.stat().st_size > MAX_IMAGE:
                    raise ValueError("invalid_tryon_image")
                key = (entry["product_id"], entry["color"])
                if key in self.prepared:
                    raise ValueError("duplicate_tryon_image")
                self.prepared[key] = jpeg(path.read_bytes())

    async def create(self, tenant, product_id, color):
        if self.mode == "disabled" or tenant != self.tenant:
            return None
        card = self.catalog.card(tenant, product_id)
        if color not in card["colors"]:
            return None
        if self.mode == "prepared":
            data = self.prepared.get((product_id, color))
        else:
            # An optional worker not ready must not quarantine otherwise healthy voice services.
            try:
                health = json.loads(
                    await self.backend._request("GET", "http://127.0.0.1:8083/health")
                )
            except (httpx.HTTPError, RuntimeError, ValueError):
                return None
            if not health.get("ready") or health.get("busy"):
                return None
            variant = next(v for v in card["variants"] if v["color"] == color)
            path = variant["image_path"] or card["image_path"]
            garment, _ = self.catalog.image(tenant, path.rsplit("/", 1)[-1])
            data = await self.backend._request(
                "POST",
                "http://127.0.0.1:8083/try-on",
                inference=True,
                limit=MAX_IMAGE,
                files={
                    "person": ("person.jpg", self.reference, "image/jpeg"),
                    "garment": ("garment.jpg", garment, "image/jpeg"),
                },
                data={"garment_name": card["name"], "color": color},
            )
            data = jpeg(data)
        if data is None:
            return None
        return {
            "image": data,
            "product_id": product_id,
            "color": color,
            "kind": self.mode,
            "label": "Prepared AI preview" if self.mode == "prepared" else "AI preview",
            "note": "A visual preview, not a size or fit prediction.",
        }
