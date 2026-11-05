"""Local gateway. No cloud service or NVIDIA API is emulated by this server."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import wave
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from PIL import Image
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.staticfiles import StaticFiles

from .admission import State
from .auth import Credentials
from .catalog_repository import default_repository
from .conversation import Conversation
from .errors import Conflict, Denied
from .media import MAX_AUDIO, MAX_IMAGE, sanitize_image, validate_audio
from .models.mock import MockBackend
from .models.nvidia import NvidiaBackend
from .pipeline import Message, run_message, run_turn
from .settings import GPUSettings
from .tryon import TryOn
from .walkthrough import FILES as WALKTHROUGH_FILES
from .walkthrough import load as load_walkthrough

MAX_BODY = 2_700_000
MAX_MESSAGE_BODY = 700_000
MAX_TEXT = 2000
MESSAGE_FIELDS = {"audio", "text", "selected_product_id", "display_revision"}
PRODUCT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class MemoryMultipart(MultiPartParser):
    # The total body cap is smaller than this spool threshold: no media spills to disk.
    max_file_size = max(MAX_BODY, 2 * MAX_IMAGE + 16384) + 1
    spool_max_size = max_file_size


async def bounded_body(request, limit):
    data = bytearray()

    async def read():
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise HTTPException(413, "request_too_large")
            data.extend(chunk)

    try:
        await asyncio.wait_for(read(), 15)
    except TimeoutError:
        raise HTTPException(408, "upload_deadline") from None
    return bytes(data)


async def object_body(request, allowed):
    try:
        body = json.loads(await bounded_body(request, 4096))
    except (ValueError, UnicodeError):
        raise HTTPException(400, "invalid_json") from None
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise HTTPException(400, "unexpected_fields")
    return body


def idempotency(request):
    value = request.headers.get("idempotency-key", "")
    if not value.isascii() or not 1 <= len(value) <= 128 or any(c.isspace() for c in value):
        raise HTTPException(400, "invalid_idempotency_key")
    return value


def query_fields(body):
    if set(body) != {"sku", "store_id"} or not all(
        isinstance(v, str) and 1 <= len(v) <= 80 for v in body.values()
    ):
        raise HTTPException(400, "sku_and_store_required")
    return body["sku"], body["store_id"]


def fingerprint_of(*parts):
    """Bind an idempotency key to the exact request content."""
    digest = hashlib.sha256()
    for part in parts:
        data = part if isinstance(part, bytes) else json.dumps(part).encode()
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


async def parse_form(request, limit, **limits):
    data = await bounded_body(request, limit)
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(400, "invalid_multipart")

    async def stream():
        yield data

    try:
        return await MemoryMultipart(request.headers, stream(), **limits).parse()
    except (MultiPartException, ValueError):
        raise HTTPException(400, "invalid_multipart") from None


def fixture_media():
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "NONE"))
        wav.writeframes(bytes(3200))
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "navy").save(image, "PNG")
    return audio.getvalue(), image.getvalue()


def create_app(
    credentials=None, operator=None, state=None, backend=None, deadline=30, catalog=None, tryon=None
):
    mode = os.getenv("DEMO_MODE")
    if mode not in {"mock", "nvidia"}:
        raise RuntimeError("Set DEMO_MODE=mock or DEMO_MODE=nvidia explicitly")
    auth = Credentials(credentials, operator)
    # A broken seed or image manifest stops startup here, before any route exists.
    catalog = catalog if catalog is not None else default_repository()
    state = state if state is not None else State()
    if backend is None:
        backend = MockBackend() if mode == "mock" else NvidiaBackend(GPUSettings.from_env())
    if backend.mode != mode:
        raise RuntimeError("Backend must match explicit mode")
    if isinstance(backend, NvidiaBackend):
        deadline = backend.settings.turn_timeout_seconds
    tryon = tryon or TryOn(catalog, backend)
    # The optional recorded walkthrough. A set but broken directory stops startup here.
    walkthrough = load_walkthrough(os.getenv("WALKTHROUGH_DIR"))
    tasks: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        if not await backend.ready():
            await backend.close()
            raise RuntimeError("Configured model backends are not ready; run preflight")

        async def reap():
            while True:
                await asyncio.sleep(5)
                with state.lock:
                    state._clean()

        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            pending = list(tasks.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await backend.close()
            state.sessions.clear()
            state.requests.clear()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.admission = state

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(Denied)
    async def denied(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def session(request, sid):
        token = auth.bearer(request.headers.get("authorization"))
        return state.authorize(sid, token), token

    @app.get("/livez")
    async def live():
        return {"live": True, "mode": mode}

    # The configured model of each stage, as the events label them. Public: model ids and
    # revisions are the pinned profile, not an attestation that this hardware ran them.
    stages = {
        name: backend.stage_metadata(event)
        for name, event in (
            ("asr", "asr.completed"),
            ("model", "model.completed"),
            ("tts", "tts.completed"),
        )
    }

    @app.get("/v1/info")
    async def info():
        return {
            "mode": mode,
            "site": backend.site_id,
            "region": backend.region,
            "pipeline": "pipecat",
            "fixture_enabled": mode == "mock",
            "profile_digest": backend.profile_digest,
            "context_tokens": backend.context_tokens,
            "stages": stages,
            "walkthrough": walkthrough is not None,
            "tryon": {"mode": tryon.mode},
        }

    @app.get("/v1/catalog")
    async def storefront(request: Request):
        """Scoped storefront for browsing. Never reserves a session or touches a model."""
        return catalog.storefront(auth.tenant(request.headers.get("authorization")))

    @app.get("/v1/catalog/products/{product_id}")
    async def product(product_id: str, request: Request):
        tenant = auth.tenant(request.headers.get("authorization"))
        try:
            return catalog.product_detail(tenant, product_id)
        except Denied:
            # Unknown and foreign products get the same answer.
            raise HTTPException(404, "product_not_found") from None

    @app.get("/v1/catalog/images/{image_id}")
    async def image(image_id: str, request: Request):
        tenant = auth.tenant(request.headers.get("authorization"))
        try:
            data, media_type = catalog.image(tenant, image_id)
        except Denied:
            raise HTTPException(404, "image_not_found") from None
        return Response(data, media_type=media_type)

    @app.get("/admin/ready")
    async def ready(request: Request):
        auth.require_operator(request.headers.get("authorization"))
        # Backend health and occupancy are separate: a busy GPU is not an unhealthy backend.
        healthy = await backend.healthy()
        return JSONResponse(
            {
                "mode": mode,
                "site": backend.site_id,
                "ready": healthy,
                "backend_ready": healthy,
                "profile_digest": backend.profile_digest,
            }
            | state.snapshot(),
            status_code=200 if healthy else 503,
        )

    @app.post("/v1/sessions")
    async def start(request: Request):
        tenant = auth.tenant(request.headers.get("authorization"))
        key = idempotency(request)
        body = await object_body(request, {"capability_profile"})
        if body.get("capability_profile", "retail-voice-v1") != "retail-voice-v1":
            raise HTTPException(400, "unsupported_profile")
        if not await backend.healthy():
            raise HTTPException(503, "backend_unready_or_quarantined")
        s = state.reserve(tenant, key)
        with state.lock:
            if s.conversation is None:
                s.conversation = Conversation(s.tenant, catalog.default_store(s.tenant))
        retained = state.snapshot()["retained_sessions"]
        return {
            "session_id": s.id,
            "session_token": s.token,
            "tenant": s.tenant,
            "site": backend.site_id,
            "mode": mode,
            "reason": "local_capacity_available" if mode == "mock" else "site_capacity_available",
            "reservation_seconds_remaining": max(0, s.reserved_until - state.clock()),
            "idle_timeout_seconds": state.idle_ttl,
            "max_turns": state.max_turns,
            "retained_sessions": {"used": retained["used"], "max": retained["max"]},
        }

    @app.delete("/v1/sessions/{sid}")
    async def end(sid: str, request: Request):
        _, token = session(request, sid)
        state.end(sid, token)
        task = tasks.get(sid)
        if task:
            if not task.cancelling():
                task.cancel()
            await task
        return {"ended": True}

    def public_result(r, sid):
        tid = r["turn_id"]
        result = {
            k: v for k, v in r.items() if k not in {"audio", "events", "commit", "preview_image"}
        } | {
            "site": backend.site_id,
            "audio_path": f"/v1/sessions/{sid}/turns/{tid}/audio" if r["audio"] else None,
            "audio_kind": ("test_tone" if mode == "mock" else "synthesized_speech")
            if r["audio"]
            else None,
        }
        if result.get("preview"):
            result["preview"] = result["preview"] | {
                "image_path": f"/v1/sessions/{sid}/turns/{tid}/preview"
            }
        return result

    def reply_frame(r, sid):
        # A transport frame, never a trace event or log entry. Only the authenticated stream
        # receives it. This removes the result/audio HTTP round trip after turn.completed.
        return (
            json.dumps(
                {
                    "type": "reply",
                    "result": public_result(r, sid),
                    "audio_base64": base64.b64encode(r["audio"]).decode("ascii"),
                }
            )
            + "\n"
        )

    async def respond(sid, token, key, fingerprint, runner, inline=False):
        """Admit one turn, stream its events, keep capacity until the work really stops."""
        current = state.authorize(sid, token)
        if key not in current.turns and not await backend.healthy():
            raise HTTPException(503, "backend_unready_or_quarantined")
        s, cached = state.begin(sid, token, key, fingerprint)
        if cached is not None:
            return Response(
                "".join(json.dumps(e) + "\n" for e in cached["events"])
                + (reply_frame(cached, sid) if inline else ""),
                media_type="application/x-ndjson",
            )
        permit = s.ticket
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def execute():
            result = None
            try:
                result = await runner(s, queue.put_nowait, permit)
            finally:
                state.finish(s, result)
                tasks.pop(s.id, None)
                queue.put_nowait(None)

        task = asyncio.create_task(execute())
        tasks[sid] = task

        async def events():
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield json.dumps(event) + "\n"
                if inline and key in s.turns:
                    yield reply_frame(s.turns[key], sid)
            finally:
                if not task.done() and not task.cancelling():
                    task.cancel()
                # Shield cleanup from response cancellation. Capacity remains held until done.
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(task)

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.post("/v1/sessions/{sid}/fixture-turn")
    async def fixture(sid: str, request: Request):
        if mode != "mock":
            raise HTTPException(404, "fixture_unavailable")
        _, token = session(request, sid)
        key = idempotency(request)
        sku, store = query_fields(await object_body(request, {"sku", "store_id"}))
        audio, image = fixture_media()
        return await diagnostic(sid, token, key, audio, image, sku, store)

    async def diagnostic(sid, token, key, audio, image, sku, store):
        return await respond(
            sid,
            token,
            key,
            fingerprint_of(audio, image, [sku, store]),
            lambda s, emit, permit: run_turn(
                backend, s, audio, image, sku, store, deadline, emit, permit
            ),
        )

    @app.post("/v1/sessions/{sid}/turns")
    async def turn(sid: str, request: Request):
        _, token = session(request, sid)
        key = idempotency(request)
        form = await parse_form(request, MAX_BODY, max_files=2, max_fields=2, max_part_size=80)
        try:
            if len(form.multi_items()) != 4 or set(form) != {"audio", "image", "sku", "store_id"}:
                raise HTTPException(400, "audio_image_sku_store_required")
            sku, store = query_fields({"sku": form["sku"], "store_id": form["store_id"]})
            audio_file, image_file = form["audio"], form["image"]
            if not isinstance(audio_file, UploadFile) or not isinstance(image_file, UploadFile):
                raise HTTPException(400, "media_files_required")
            audio = await audio_file.read(MAX_AUDIO + 1)
            image = await image_file.read(MAX_IMAGE + 1)
            if len(audio) > MAX_AUDIO or len(image) > MAX_IMAGE:
                raise HTTPException(413, "media_too_large")
            try:
                audio, image = validate_audio(audio), sanitize_image(image)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        finally:
            await form.close()
        return await diagnostic(sid, token, key, audio, image, sku, store)

    @app.post("/v1/sessions/{sid}/messages")
    async def message(sid: str, request: Request):
        """One shopper message: exactly one of audio or text, plus optional card references."""
        current, token = session(request, sid)
        key = idempotency(request)
        form = await parse_form(
            request, MAX_MESSAGE_BODY, max_files=1, max_fields=3, max_part_size=4 * MAX_TEXT
        )
        try:
            names = [name for name, _ in form.multi_items()]
            if len(names) != len(set(names)) or set(names) - MESSAGE_FIELDS:
                raise HTTPException(400, "unexpected_fields")
            if ("audio" in names) == ("text" in names):
                raise HTTPException(400, "audio_or_text_required")
            audio = text = None
            if "audio" in names:
                upload = form["audio"]
                if not isinstance(upload, UploadFile):
                    raise HTTPException(400, "audio_file_required")
                audio = await upload.read(MAX_AUDIO + 1)
                if len(audio) > MAX_AUDIO:
                    raise HTTPException(413, "media_too_large")
                try:
                    audio = validate_audio(audio)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from None
            else:
                value = form["text"]
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_TEXT:
                    raise HTTPException(400, "text_required_up_to_2000_characters")
                text = value.strip()
            selected = form.get("selected_product_id")
            if selected is not None and (
                not isinstance(selected, str)
                or not PRODUCT_ID.fullmatch(selected)
                or not catalog.owns_product(current.tenant, selected)
            ):
                # Unknown and foreign products get the same answer.
                raise HTTPException(400, "invalid_selected_product")
            revision = form.get("display_revision")
            if revision is not None:
                if not isinstance(revision, str) or not re.fullmatch(r"[0-9]{1,6}", revision):
                    raise HTTPException(400, "invalid_display_revision")
                revision = int(revision)
        finally:
            await form.close()
        shopper = Message(
            audio=audio, text=text, selected_product_id=selected, display_revision=revision
        )
        fingerprint = fingerprint_of(
            "message",
            audio or b"",
            text or "",
            selected or "",
            -1 if revision is None else revision,
        )
        return await respond(
            sid,
            token,
            key,
            fingerprint,
            lambda s, emit, permit: run_message(
                backend, s, shopper, catalog, deadline, emit, permit, tryon=tryon
            ),
            inline=request.headers.get("x-inline-reply") == "1",
        )

    def result_for(request, sid, tid):
        s, _ = session(request, sid)
        result = s.results.get(tid)
        if result is None:
            raise HTTPException(404, "result_unavailable")
        return result

    @app.get("/v1/sessions/{sid}/turns/{tid}/result")
    async def result(sid: str, tid: str, request: Request):
        return public_result(result_for(request, sid, tid), sid)

    @app.get("/v1/sessions/{sid}/turns/{tid}/audio")
    async def audio(sid: str, tid: str, request: Request):
        r = result_for(request, sid, tid)
        if not r["audio"]:
            raise HTTPException(404, "audio_unavailable")
        return Response(r["audio"], media_type="audio/wav")

    @app.get("/v1/sessions/{sid}/turns/{tid}/preview")
    async def preview(sid: str, tid: str, request: Request):
        r = result_for(request, sid, tid)
        if not r.get("preview_image"):
            raise HTTPException(404, "preview_unavailable")
        return Response(r["preview_image"], media_type="image/jpeg")

    @app.put("/admin/admission")
    async def drain(request: Request):
        auth.require_operator(request.headers.get("authorization"))
        body = await object_body(request, {"drain"})
        if set(body) != {"drain"} or type(body["drain"]) is not bool:
            raise HTTPException(400, "drain_boolean_required")
        with state.lock:
            state.drain = body["drain"]
        return {
            "event_type": "admission.changed",
            "actor": "operator",
            "drain": state.drain,
            "mode": mode,
        }

    web = Path(__file__).parent / "web"

    @app.api_route("/app/walkthrough/{name}", methods=["GET", "HEAD"])
    async def walkthrough_file(name: str):
        """The recorded walkthrough's three files, without a key like the other page assets.
        Range requests work, so the player can seek to a chapter, and HEAD answers like GET
        without a body. Registered before the static mount, so nothing else is served from
        this path."""
        if walkthrough is None or name not in WALKTHROUGH_FILES:
            raise HTTPException(404, "walkthrough_unavailable")
        return FileResponse(walkthrough["files"][name], media_type=WALKTHROUGH_FILES[name])

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    app.mount("/app", StaticFiles(directory=web), name="app")
    return app
