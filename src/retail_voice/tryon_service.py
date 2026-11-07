"""Optional loopback image worker. Loads an operator-downloaded model, never a hosted API."""

import asyncio
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from PIL import Image
from starlette.datastructures import UploadFile

from .api import parse_form
from .media import MAX_IMAGE, sanitize_image
from .tryon import MODEL_ID, MODEL_REVISION


class ImageRuntime:
    def __init__(self):
        self.lock = threading.Lock()
        self.pipeline: Any = None

    def load(self):
        import torch
        from diffusers import Flux2KleinPipeline

        root = Path(os.environ["TRYON_MODEL_DIR"])
        if not root.is_absolute() or not root.is_dir() or not torch.cuda.is_available():
            raise RuntimeError("local_tryon_model_and_cuda_required")
        receipt = json.loads((root / "tryon-model.json").read_text())
        if receipt != {"model_id": MODEL_ID, "revision": MODEL_REVISION}:
            raise RuntimeError("tryon_model_revision_mismatch")
        # Loading never downloads weights or sends reference photos outside the host.
        self.pipeline = Flux2KleinPipeline.from_pretrained(
            str(root), torch_dtype=torch.bfloat16, local_files_only=True
        )
        self.pipeline.enable_model_cpu_offload()

    def run(self, person, garment, name, color):
        import torch

        if not self.lock.acquire(blocking=False):
            raise RuntimeError("tryon_busy")
        try:
            references = []
            for data in (person, garment):
                with Image.open(io.BytesIO(data)) as image:
                    references.append(image.convert("RGB"))
            prompt = (
                f"Show the person from image 1 wearing the {name} in {color} from image 2. "
                "Preserve the person's face, hair, body proportions, pose, trousers, shoes, "
                "background, and any accessories already visible in image 1. "
                "Do not introduce new accessories. Replace only the upper garment. Match the garment's "
                "color, cut, closures and fabric. Photorealistic full-body clothing preview. "
                "No text, no logos added, no extra people."
            )
            with torch.inference_mode():
                result = self.pipeline(
                    image=references,
                    prompt=prompt,
                    height=1024,
                    width=768,
                    guidance_scale=1.0,
                    num_inference_steps=4,
                    generator=torch.Generator(device="cuda").manual_seed(42),
                ).images[0]
            out = io.BytesIO()
            result.save(out, "JPEG", quality=90)
            if len(out.getvalue()) > MAX_IMAGE:
                raise RuntimeError("tryon_output_too_large")
            return out.getvalue()
        finally:
            self.lock.release()


def create_app(runtime=None):
    runtime = runtime or ImageRuntime()

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(runtime.load)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {
            "ready": runtime.pipeline is not None,
            "busy": runtime.lock.locked(),
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
        }

    @app.post("/try-on")
    async def preview(request: Request):
        form = await parse_form(
            request, 2 * MAX_IMAGE + 16384, max_files=2, max_fields=2, max_part_size=160
        )
        try:
            if len(form.multi_items()) != 4 or set(form) != {
                "person",
                "garment",
                "garment_name",
                "color",
            }:
                raise HTTPException(400, "invalid_tryon_fields")
            images = []
            for key in ("person", "garment"):
                upload = form[key]
                if not isinstance(upload, UploadFile):
                    raise HTTPException(400, "image_required")
                try:
                    images.append(sanitize_image(await upload.read(MAX_IMAGE + 1)))
                except ValueError:
                    raise HTTPException(400, "invalid_image") from None
            name, color = form["garment_name"], form["color"]
            if not all(isinstance(s, str) and 1 <= len(s) <= 80 for s in (name, color)):
                raise HTTPException(400, "invalid_garment")
        finally:
            await form.close()
        task = asyncio.create_task(
            asyncio.to_thread(runtime.run, images[0], images[1], name, color)
        )
        try:
            return Response(await asyncio.shield(task), media_type="image/jpeg")
        except asyncio.CancelledError:
            await task
            raise
        except Exception:
            raise HTTPException(503, "tryon_failed") from None

    return app
