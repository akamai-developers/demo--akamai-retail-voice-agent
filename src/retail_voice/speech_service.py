"""Private NeMo service. Start only on loopback, in a separate CUDA environment."""

import asyncio
import io
import threading
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from .media import MAX_AUDIO, validate_audio
from .settings import GPUSettings


class NoSpeech(Exception):
    """The audio held no usable speech. The device is fine."""


class SpeechRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.models = {}

    def load(self):
        import torch
        from nemo.collections.asr.models import ASRModel
        from nemo.collections.tts.models.magpietts import MagpieTTSModel

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required; CPU fallback disabled")
        if not all(self.settings.artifact(k).is_file() for k in ("asr", "tts", "codec")):
            raise RuntimeError("Pinned speech artifacts are missing")
        asr = ASRModel.restore_from(str(self.settings.artifact("asr"))).cuda().eval()
        cfg = MagpieTTSModel.restore_from(str(self.settings.artifact("tts")), return_config=True)
        cfg.codecmodel_path = str(self.settings.artifact("codec"))
        tts = (
            MagpieTTSModel.restore_from(
                str(self.settings.artifact("tts")), override_config_path=cfg
            )
            .cuda()
            .eval()
        )
        self.models.update(asr=asr, tts=tts)

    def run(self, operation, value):
        import torch

        if not self.lock.acquire(blocking=False):
            raise RuntimeError("speech_busy")
        try:
            with torch.inference_mode():
                return getattr(self, operation)(value)
        finally:
            self.lock.release()

    def device_check(self):
        """Raise if CUDA no longer answers. Called after a model exception to tell a rejected
        input apart from a broken device."""
        import torch

        torch.cuda.synchronize()

    def recognize(self, data):
        import numpy as np

        with wave.open(io.BytesIO(data)) as wav:
            samples = (
                np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
                / 32768.0
            )
        # Pinned NeMo accepts arrays; avoid writing uploaded audio to a temporary WAV.
        try:
            output = self.models["asr"].transcribe(
                [samples], batch_size=1, num_workers=0, verbose=False, use_lhotse=False
            )
        except Exception as exc:
            # The pinned ASR raises on silence, a tone or noise. With the device still answering
            # that is a rejected input, not a GPU failure, so the gateway must not quarantine.
            self.device_check()
            raise NoSpeech("no_speech") from exc
        if isinstance(output, tuple):
            output = output[0]
        text = output[0] if isinstance(output[0], str) else output[0].text
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise NoSpeech("no_speech")
        return {"text": text.strip()}

    def synthesize(self, text):
        import soundfile as sf

        model = self.models["tts"]
        audio, lengths = model.do_tts(
            text,
            language="en",
            apply_TN=False,
            use_cfg=True,
            speaker_index=self.settings.speaker_index,
        )
        samples = audio[0, : int(lengths[0].item())].float().cpu().numpy()
        if not 0 < len(samples) <= model.output_sample_rate * 90:
            raise RuntimeError("speech_output_too_long")
        out = io.BytesIO()
        sf.write(out, samples, model.output_sample_rate, format="WAV", subtype="PCM_16")
        return out.getvalue()


async def bounded(request, limit):
    async def read():
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise HTTPException(413, "request_too_large")
            data.extend(chunk)
        return bytes(data)

    try:
        return await asyncio.wait_for(read(), 15)
    except TimeoutError:
        raise HTTPException(408, "upload_deadline") from None


def create_app(settings=None, runtime=None):
    settings = settings or GPUSettings.from_env()
    runtime = runtime or SpeechRuntime(settings)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(runtime.load)
        yield
        runtime.models.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def infer(operation, value):
        task = asyncio.create_task(asyncio.to_thread(runtime.run, operation, value))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The worker owns the lock until CUDA really returns.
            await task
            raise
        except NoSpeech:
            # A 400 tells the gateway the model refused the input and nothing is left running.
            raise HTTPException(400, "no_speech") from None
        except Exception:
            raise HTTPException(503, "speech_inference_failed") from None

    @app.get("/health")
    async def health():
        return {
            "ready": len(runtime.models) == 2,
            "busy": runtime.lock.locked(),
            "device": "cuda",
            "profile_digest": settings.profile_digest,
        }

    @app.post("/asr")
    async def asr(request: Request):
        data = await bounded(request, MAX_AUDIO)
        try:
            validate_audio(data)
        except ValueError:
            raise HTTPException(400, "invalid_audio") from None
        return await infer("recognize", data)

    @app.post("/tts")
    async def tts(request: Request):
        import json

        try:
            body = json.loads(await bounded(request, 8192))
            if (
                not isinstance(body, dict)
                or set(body) != {"text"}
                or not isinstance(body["text"], str)
                or not 1 <= len(body["text"]) <= 1200
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid_text") from None
        return Response(await infer("synthesize", body["text"]), media_type="audio/wav")

    return app
