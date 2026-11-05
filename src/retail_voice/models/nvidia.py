"""NVIDIA NeMo speech + local vLLM. No hosted API and no mock fallback."""

import asyncio
import io
import json
import os
import wave

import httpx

from ..conversation import REPLY_TOKENS
from ..settings import model_manifest
from ..speech_text import speech_chunks
from .base import Decision

MARKUP = ("<think>", "</think>", "<tool_call>", "<|tool_call|>")
# Greedy decoding: two GPU conversation smokes in a row chose identical tools.
TEMPERATURE = 0.0


class ModelRejected(RuntimeError):
    """A model service refused the request with HTTP 400 before running it."""


def validate_reply_audio(data):
    if not 44 < len(data) <= 8_000_000:
        raise ValueError("invalid_speech_size")
    with wave.open(io.BytesIO(data)) as wav:
        frames = wav.getnframes()
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getcomptype() != "NONE"
            or not 8000 <= wav.getframerate() <= 48000
            or not 0 < frames <= wav.getframerate() * 90
            or len(wav.readframes(frames)) != frames * 2
        ):
            raise ValueError("invalid_speech_wav")
    return data


class NvidiaBackend:
    mode = "nvidia"
    speech_url = "http://127.0.0.1:8082"
    vlm_url = "http://127.0.0.1:8081/v1"
    served_model = "retail-nemotron"

    def __init__(self, settings, client=None):
        self.settings = settings
        self.site_id, self.region = settings.site_id, settings.region
        self.profile_digest = settings.profile_digest
        self.context_tokens = settings.context_tokens
        self.manifest = model_manifest()
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, trust_env=False, follow_redirects=False
        )
        self.quarantined = settings.quarantine_path.exists()
        # Inference requests this process has in flight. While one runs, the marker on disk is
        # ours and the speech service may be busy; neither means the backend is unhealthy.
        self.inflight = 0

    async def close(self):
        await self.client.aclose()

    def stage_metadata(self, stage):
        key = {"asr.completed": "asr", "model.completed": "vlm", "tts.completed": "tts"}[stage]
        model = self.manifest["models"][key]
        return {"model_id": model["id"], "model_revision": model["revision"]}

    def mark_inference(self):
        # Persist before sending work, so a gateway crash cannot lose the quarantine.
        # An unwritable marker fails before any request reaches a model service.
        with self.settings.quarantine_path.open("x") as marker:
            marker.write(
                json.dumps({"reason": "inference_in_flight", "profile_digest": self.profile_digest})
                + "\n"
            )
            marker.flush()
            os.fsync(marker.fileno())

    async def _request(self, method, url, *, limit=131072, inference=False, **kwargs):
        if self.quarantined or (self.settings.quarantine_path.exists() and not self.inflight):
            raise RuntimeError("backend_quarantined")
        counted = False
        try:
            if inference:
                self.inflight += 1
                counted = True
                self.mark_inference()
            async with self.client.stream(method, url, **kwargs) as response:
                if response.status_code == 400 and inference:
                    # vLLM and the speech service validate a request before running it, so
                    # a 400 is a definitive refusal: no GPU work started and the marker can go.
                    self.settings.quarantine_path.unlink()
                    raise ModelRejected("model_rejected_request")
                # Do not follow redirects, even to another local endpoint.
                if not 200 <= response.status_code < 300:
                    raise RuntimeError("backend_http_failure")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise ValueError("backend_response_too_large")
                    data.extend(chunk)
                if inference:
                    self.settings.quarantine_path.unlink()
                return bytes(data)
        except BaseException as exc:
            if (
                inference
                and isinstance(exc, (asyncio.CancelledError, Exception))
                and not isinstance(exc, ModelRejected)
            ):
                self.quarantined = True
            raise
        finally:
            if counted:
                self.inflight -= 1

    async def ready(self):
        """Startup and preflight: services loaded, idle, matching profile, nothing quarantined."""
        return await self._services(require_idle=True)

    async def healthy(self):
        """Admission: quarantine fails it; this process's own running turn does not."""
        if self.quarantined:
            return False
        if self.inflight:
            return True
        return await self._services(require_idle=False)

    async def _services(self, require_idle):
        if not self.settings.runtime_dir.is_dir() or not os.access(
            self.settings.runtime_dir, os.W_OK
        ):
            return False
        try:
            health = json.loads(await self._request("GET", self.speech_url + "/health"))
            models = json.loads(await self._request("GET", self.vlm_url + "/models"))
            return (
                health.get("ready") is True
                and (health.get("busy") is False or not require_idle)
                and health.get("device") == "cuda"
                and health.get("profile_digest") == self.profile_digest
                and any(m.get("id") == self.served_model for m in models.get("data", []))
            )
        except Exception:
            return False

    async def transcribe(self, audio):
        result = json.loads(
            await self._request(
                "POST",
                self.speech_url + "/asr",
                content=audio,
                headers={"Content-Type": "audio/wav"},
                inference=True,
            )
        )
        text = result.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise ValueError("invalid_transcript")
        return text.strip()

    async def decide(self, messages, tools, tool_choice="auto"):
        """One chat completion. The caller builds the messages and enforces the turn policy."""
        payload = {
            "model": self.served_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
            "stream": False,
            "temperature": TEMPERATURE,
            "max_tokens": REPLY_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        result = json.loads(
            await self._request(
                "POST", self.vlm_url + "/chat/completions", json=payload, inference=True
            )
        )
        choices = result.get("choices", [])
        if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
            raise ValueError("incomplete_model_response")
        reported = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        usage = {
            key: reported[key]
            for key in ("prompt_tokens", "completion_tokens")
            if type(reported.get(key)) is int and reported[key] >= 0
        } or None
        message = choices[0]["message"]
        calls = message.get("tool_calls") or []
        if calls:
            if len(calls) != 1 or tool_choice == "none":
                raise ValueError("unexpected_tool_calls")
            call = calls[0]
            if (
                call.get("type") != "function"
                or not isinstance(call.get("id"), str)
                or not 1 <= len(call["id"]) <= 128
            ):
                raise ValueError("invalid_tool_call")
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ValueError("invalid_tool_call")
            try:
                arguments = json.loads(function.get("arguments") or "")
            except (TypeError, ValueError):
                arguments = None
            if not isinstance(arguments, dict):
                # Not a JSON object. The loop reports bad arguments back to the model and
                # lets it retry, instead of failing the turn on output greedy decoding repeats.
                arguments = None
            return Decision(
                tool=function["name"], arguments=arguments, call_id=call["id"], usage=usage
            )
        answer = message.get("content")
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 1200:
            raise ValueError("missing_tool_or_answer")
        # Never recover tool markup/reasoning from ordinary text with regex parsing. The loop
        # withholds the text and lets the model try once more.
        if any(marker in answer for marker in MARKUP):
            return Decision(malformed="unexpected_model_markup", usage=usage)
        return Decision(answer=answer.strip(), usage=usage)

    async def synthesize(self, text):
        replies = []
        for chunk in speech_chunks(text):
            data = await self._request(
                "POST",
                self.speech_url + "/tts",
                json={"text": chunk},
                limit=8_000_000,
                inference=True,
            )
            replies.append(validate_reply_audio(data))
        if len(replies) == 1:
            return replies[0]
        # Usually one WAV. Long normalized replies retain every sentence and play as one
        # file, without browser segment switches or sample-rate changes.
        out = io.BytesIO()
        params = None
        with wave.open(out, "wb") as joined:
            for index, data in enumerate(replies):
                with wave.open(io.BytesIO(data)) as source:
                    fmt = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                    if params is None:
                        params = fmt
                        joined.setparams((*fmt, 0, "NONE", "not compressed"))
                    elif fmt != params:
                        raise ValueError("speech_format_changed")
                    if index:
                        joined.writeframes(b"\0" * (int(fmt[2] * 0.12) * fmt[0] * fmt[1]))
                    joined.writeframes(source.readframes(source.getnframes()))
        return validate_reply_audio(out.getvalue())
