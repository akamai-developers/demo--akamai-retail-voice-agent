"""Measure one fixed shopping workload against a gateway.

Each trial opens a fresh session for one retailer, runs five turns the way the browser does
(greeting, rain jacket search, "the second one" in navy, a stock question, one spoken turn from a
WAV file), then signs out. For every turn it records wall times on this machine's monotonic clock,
measured from the request start: first event byte, turn end, result received, audio received. Beside
them it records the gateway's own stage durations from the turn's events. The two clocks are never
subtracted from each other, and no difference is called a network round trip.

The report holds statuses, tool names, counts, sizes and durations: no transcript, answer, key or
token. It needs the tenant key in TENANT_A_KEY or TENANT_B_KEY and works against mock mode too.

    TENANT_A_KEY=... python tools/measure_latency.py --base-url http://127.0.0.1:8767 \\
        --audio shopping-rain.wav --trials 3 --location "test workstation" --build 08760dc \\
        --output latency.json
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import statistics
import sys
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# The fixed workload, in order. None means the spoken turn from --audio.
STEPS: list[tuple[str, str | None]] = [
    ("greeting", "Hi there"),
    ("search", "I need a lightweight rain jacket under 150 dollars"),
    ("second_one", "Does the second one come in navy?"),
    ("stock", "Is medium available here?"),
    ("voice", None),
]
WALL = ("first_event", "turn_end", "result", "audio")


class SlotHeld(Exception):
    """The retailer's one session slot is held by another conversation."""


def check_origin(base_url):
    """Keys only travel over HTTPS, or plain HTTP to this machine (an SSH tunnel)."""
    origin = urlsplit(base_url)
    loopback = origin.scheme == "http" and origin.hostname in {"127.0.0.1", "localhost"}
    if origin.username or origin.password or origin.query or origin.fragment:
        raise ValueError("base URL must be a bare origin")
    if origin.path not in {"", "/"} or not (origin.scheme == "https" or loopback):
        raise ValueError("base URL must be an HTTPS origin or loopback HTTP")


def audio_facts(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        seconds = wav.getnframes() / wav.getframerate()
    return {
        "bytes": len(data),
        "seconds": round(seconds, 2),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def stage_durations(events):
    """The gateway's own durations for one turn, from its events (gateway clock)."""
    found: dict[str, Any] = {"decisions": [], "tools": []}
    for event in events:
        kind = event.get("event_type")
        if kind == "turn.waiting" and "queued_ms" in event:
            found["queued_ms"] = event["queued_ms"]
        elif kind == "asr.completed":
            found["asr_ms"] = event.get("duration_ms")
        elif kind == "model.completed":
            found["decisions"].append(
                {"decision": event.get("decision"), "ms": event.get("duration_ms")}
                | {k: event[k] for k in ("prompt_tokens", "completion_tokens") if k in event}
            )
        elif kind in {"tool.completed", "tool.denied"}:
            found["tools"].append(
                {
                    "name": event.get("tool_name"),
                    "outcome": "completed" if kind == "tool.completed" else "denied",
                    "by": "gateway"
                    if event.get("tool_call_id", "").startswith("gateway-")
                    else "model",
                    "ms": event.get("duration_ms"),
                }
            )
        elif kind == "tts.completed":
            found["tts_ms"] = event.get("duration_ms")
        elif kind == "preview.completed":
            found["preview_ms"] = event.get("duration_ms")
            found["preview_cache_hit"] = event.get("cache_hit", False)
        elif kind in {"turn.completed", "turn.failed"}:
            # Present from audio prefetch on: turn start to result on the gateway, queue included.
            found["gateway_total_ms"] = event.get("duration_ms")
    found["model_ms"] = round(sum(d["ms"] or 0 for d in found["decisions"]), 1)
    return found


async def turn(client, session, text=None, voice=None, audio_fetch="auto"):
    """One message as the browser sends it, timed on this machine's monotonic clock."""
    headers = {"Authorization": "Bearer " + session["session_token"]}
    parts: dict[str, tuple[Any, ...]] = (
        {"text": (None, text)} if text is not None else {"audio": ("q.wav", voice, "audio/wav")}
    )
    if session.get("display_revision") is not None:
        parts["display_revision"] = (None, str(session["display_revision"]))
    if session.get("selected_product_id"):
        parts["selected_product_id"] = (None, session["selected_product_id"])
    marks: dict[str, float] = {}
    start = time.perf_counter()
    events: list[dict] = []
    buffer = b""
    inline = None
    async with client.stream(
        "POST",
        f"/v1/sessions/{session['session_id']}/messages",
        headers=headers
        | {"Idempotency-Key": uuid.uuid4().hex}
        | ({"X-Inline-Reply": "1"} if audio_fetch == "inline" else {}),
        files=parts,
    ) as response:
        if response.status_code != 200:
            await response.aread()
            return {"http_status": response.status_code}, {}, b""
        async for chunk in response.aiter_bytes():
            if chunk and "first_event" not in marks:
                marks["first_event"] = time.perf_counter()
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if line.strip():
                    frame = json.loads(line)
                    if frame.get("type") == "reply":
                        inline = frame
                        marks["result"] = marks["audio"] = time.perf_counter()
                        continue
                    events.append(frame)
                    if "result_path" in events[-1]:
                        marks["turn_end"] = time.perf_counter()
        if buffer.strip():
            events.append(json.loads(buffer))
            marks.setdefault("turn_end", time.perf_counter())
    terminal = events[-1]

    async def fetch_result():
        response = await client.get(terminal["result_path"], headers=headers)
        response.raise_for_status()
        marks["result"] = time.perf_counter()
        return response.json()

    async def fetch_audio(path):
        response = await client.get(path, headers=headers)
        response.raise_for_status()
        marks["audio"] = time.perf_counter()
        return response.content

    reply = b""
    # Like the browser: the audio fetch starts with the result's when the terminal event names it.
    parallel = audio_fetch in {"auto", "inline"} and bool(terminal.get("audio_path"))
    if inline:
        body = inline["result"]
        reply = base64.b64decode(inline["audio_base64"], validate=True)
    elif parallel:
        body, reply = await asyncio.gather(fetch_result(), fetch_audio(terminal["audio_path"]))
    else:
        body = await fetch_result()
        if body.get("audio_path"):
            reply = await fetch_audio(body["audio_path"])
    wall = {k: round((marks[k] - start) * 1000, 1) for k in WALL if k in marks}
    record = {
        "http_status": 200,
        "status": body.get("status"),
        "reason": body.get("reason"),
        "input_source": body.get("input_source"),
        "trace_id": body.get("trace_id"),
        "audio_fetch": "inline" if inline else "with_result" if parallel else "after_result",
        "wall_ms_from_request_start": wall,
        "gateway": stage_durations(events),
        "reply_audio_bytes": len(reply),
        "reply_words": len((body.get("answer") or "").split()),
        "spoken_words": len((body.get("spoken_answer") or body.get("answer") or "").split()),
        "tools": [e["tool_name"] for e in events if e.get("event_type") == "tool.completed"],
        "cards": len(body.get("product_cards") or []),
    }
    return record, body, reply


async def trial(client, key, voice, number, audio_fetch="auto", steps=None):
    setup = await client.post(
        "/v1/sessions",
        headers={"Authorization": "Bearer " + key, "Idempotency-Key": uuid.uuid4().hex},
        json={},
    )
    if setup.status_code == 409:
        raise SlotHeld(setup.json().get("detail", "conflict"))
    setup.raise_for_status()
    session = setup.json()
    records = []
    try:
        for step, text in steps or STEPS:
            record, body, _ = await turn(
                client, session, text, voice if text is None else None, audio_fetch
            )
            records.append({"trial": number, "step": step} | record)
            if body:
                session["display_revision"] = body.get("display_revision")
                session["selected_product_id"] = body.get("selected_product_id")
    finally:
        await client.delete(
            f"/v1/sessions/{session['session_id']}",
            headers={"Authorization": "Bearer " + session["session_token"]},
        )
    return records


def median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 1) if values else None


def summarize(turns):
    """Report attempts and failures; compute latency only from completed turns."""
    summary = {}
    for step in dict.fromkeys(t["step"] for t in turns):
        attempts = [t for t in turns if t["step"] == step]
        rows = [
            t for t in attempts if t.get("http_status") == 200 and t.get("status") == "complete"
        ]
        summary[step] = {
            "trials": len(attempts),
            "complete": len(rows),
            "failed": len(attempts) - len(rows),
            **{
                f"wall_{k}_ms": median(r["wall_ms_from_request_start"].get(k) for r in rows)
                for k in WALL
            },
            "gateway_total_ms": median(r["gateway"].get("gateway_total_ms") for r in rows),
            "asr_ms": median(r["gateway"].get("asr_ms") for r in rows),
            "model_ms": median(r["gateway"].get("model_ms") for r in rows),
            "tts_ms": median(r["gateway"].get("tts_ms") for r in rows),
            "reply_audio_bytes": median(r["reply_audio_bytes"] for r in rows),
            "reply_words": median(r["reply_words"] for r in rows),
        }
    return summary


async def measure(client, key, voice, trials, audio_fetch="auto", steps=None):
    info = await client.get("/v1/info")
    info.raise_for_status()
    turns = []
    for number in range(1, trials + 1):
        turns.extend(await trial(client, key, voice, number, audio_fetch, steps))
    return info.json(), turns


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.getenv("BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--tenant", choices=["a", "b"], default="a")
    parser.add_argument("--audio", required=True, help="mono 16 kHz PCM16 WAV for the spoken turn")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--location", required=True, help="where this machine is, as you name it")
    parser.add_argument("--build", default="unknown", help="the gateway's commit, as you know it")
    parser.add_argument(
        "--audio-fetch",
        choices=["inline", "auto", "after_result"],
        default="inline",
        help="auto fetches the audio with the result when the turn end names it, as the browser",
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    check_origin(args.base_url)
    if not 1 <= args.trials <= 20:
        raise SystemExit("--trials must be 1 to 20")
    key = os.environ.get(f"TENANT_{args.tenant.upper()}_KEY")
    if not key:
        raise SystemExit(f"Set TENANT_{args.tenant.upper()}_KEY in the environment")
    voice = Path(args.audio).read_bytes()

    async def run():
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"), timeout=180, trust_env=False
        ) as client:
            return await measure(client, key, voice, args.trials, args.audio_fetch)

    try:
        info, turns = asyncio.run(run())
    except SlotHeld as held:
        print(
            json.dumps(
                {
                    "status": "slot_held",
                    "detail": str(held),
                    "hint": "Another conversation holds this store's slot. It frees on sign-out "
                    "or after five idle minutes.",
                }
            )
        )
        return 2
    report = {
        "kind": "latency-measurement",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "location": args.location,
        "build": args.build,
        "base_url": args.base_url,
        "gateway_info": info,
        "tenant": f"retailer-{args.tenant}",
        "trials": args.trials,
        "audio_fetch": args.audio_fetch,
        "audio_file": {"name": Path(args.audio).name} | audio_facts(voice),
        "clocks": "wall_ms_from_request_start is this machine's time.perf_counter; gateway values "
        "are the gateway's own monotonic durations from its events. They are not subtracted.",
        "turns": turns,
        "summary": summarize(turns),
    }
    text = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n")
    print(json.dumps(report["summary"], indent=2))
    incomplete = [t for t in turns if t.get("status") != "complete"]
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
