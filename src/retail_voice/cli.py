"""Operator commands for one existing GPU VM. No infrastructure provisioning."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .auth import Credentials
from .media import MAX_AUDIO, MAX_IMAGE, sanitize_image, validate_audio
from .models.nvidia import NvidiaBackend, validate_reply_audio
from .settings import GPUSettings, model_manifest
from .stock import STOCK_CLAIM, stock_answer


def artifact_check(settings):
    manifest = model_manifest()
    receipt = json.loads((settings.model_root / "download-receipt.json").read_text())
    if receipt.get("models") != manifest["models"]:
        raise ValueError("Downloaded model revisions do not match the pinned profile")
    for key in ("asr", "tts", "codec"):
        if not settings.artifact(key).is_file():
            raise ValueError("Speech artifacts missing")
    if not (settings.artifact("vlm") / "config.json").is_file():
        raise ValueError("VLM snapshot missing")
    return True


def gpu_info():
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = []
    for line in result.stdout.strip().splitlines():
        name, memory, driver = [v.strip() for v in line.split(",")]
        rows.append({"name": name, "memory_mib": int(memory), "driver": driver})
    if len(rows) != 1:
        raise ValueError("This profile expects one visible GPU")
    return rows


async def preflight(settings):
    report = {
        "kind": "preflight",
        "site": settings.site_id,
        "region": settings.region,
        "profile_digest": settings.profile_digest,
        "python": platform.python_version(),
        "pipecat": version("pipecat-ai"),
        "hardware_validated": False,
        "checks": {},
    }
    try:
        report["checks"]["pinned_artifacts"] = artifact_check(settings)
    except (OSError, ValueError):
        report["checks"]["pinned_artifacts"] = False
    try:
        report["gpus"] = gpu_info()
        report["checks"]["gpu_visible"] = True
    except (OSError, ValueError, subprocess.SubprocessError):
        report["checks"]["gpu_visible"] = False
    report["checks"]["not_quarantined"] = not settings.quarantine_path.exists()
    report["checks"]["runtime_writable"] = settings.runtime_dir.is_dir() and os.access(
        settings.runtime_dir, os.W_OK
    )
    backend = NvidiaBackend(settings)
    try:
        report["checks"]["model_services_ready"] = await asyncio.wait_for(backend.ready(), 10)
    except TimeoutError:
        report["checks"]["model_services_ready"] = False
    finally:
        await backend.close()
    report["status"] = "pass" if all(report["checks"].values()) else "fail"
    report["scope"] = (
        "Configuration, device visibility and service readiness only; no warmup, fit, speech or network proof."
    )
    return report


def vlm_command(settings):
    if not settings.vllm_image:
        raise ValueError("Build and audit the model image with scripts/gpu.py install first")
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        "retail-vlm",
        "--publish",
        "127.0.0.1:8081:8081",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--shm-size",
        "2g",
        "--gpus",
        "device=0",
        "--env",
        "HF_HUB_OFFLINE=1",
        "--env",
        "TRANSFORMERS_OFFLINE=1",
        "--env",
        "VLLM_NO_USAGE_STATS=1",
        "--volume",
        f"{settings.artifact('vlm')}:/models:ro",
        "--volume",
        f"{settings.vlm_cache_dir}:/tmp",
        settings.vllm_image,
        "/models",
        "--served-model-name",
        NvidiaBackend.served_model,
        "--host",
        "0.0.0.0",
        "--port",
        "8081",
        "--trust-remote-code",
        "--max-model-len",
        str(settings.context_tokens),
        "--max-num-seqs",
        "1",
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        str(settings.gpu_memory_utilization),
        "--kv-cache-dtype",
        "fp8",
        "--limit-mm-per-prompt",
        '{"image":1,"video":0,"audio":0}',
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_coder",
        "--reasoning-parser",
        "nemotron_v3",
    ]


def download(settings, accepted):
    if not accepted:
        raise ValueError("Read the model licenses, then explicitly pass --accept-model-licenses")
    from huggingface_hub import snapshot_download

    manifest = model_manifest()
    settings.model_root.mkdir(parents=True, exist_ok=True)
    for key, model in manifest["models"].items():
        snapshot_download(
            repo_id=model["id"],
            revision=model["revision"],
            local_dir=settings.model_root / key,
            allow_patterns=[model["file"], "LICENSE*"]
            if "file" in model
            else model.get("allow_patterns"),
        )
    hashes = {}
    for key in ("asr", "tts", "codec"):
        with settings.artifact(key).open("rb") as stream:
            hashes[key] = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = {
        "models": manifest["models"],
        "speech_sha256": hashes,
        "vlm_revision": manifest["models"]["vlm"]["revision"],
        "hardware_validated": False,
    }
    (settings.model_root / "download-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return {"downloaded": True, "hardware_validated": False}


async def smoke(settings, args):
    if not args.allow_inference:
        raise ValueError("smoke-gpu requires --allow-inference and real audio/image files")
    Credentials()  # Fail before inference if the two tenant keys or operator key are missing.
    origin = urlsplit(args.gateway)
    if (
        origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
        or not (
            origin.scheme == "https"
            or (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"})
        )
    ):
        raise ValueError("Gateway must be an HTTPS origin or loopback HTTP")
    if not args.audio or not (args.image or args.conversation):
        raise ValueError(
            "Set --audio and --image to your synthetic spoken question and product photo"
        )
    with Path(args.audio).open("rb") as stream:
        audio = validate_audio(stream.read(MAX_AUDIO + 1))
    if args.conversation:
        async with httpx.AsyncClient(
            base_url=args.gateway,
            timeout=2 * settings.turn_timeout_seconds + 30,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            info = await client.get("/v1/info")
            info.raise_for_status()
            if info.json().get("mode") != "nvidia" or info.json().get("site") != settings.site_id:
                raise ValueError("GPU smoke refuses mock or wrong-site gateway")
            found = await conversation_smoke(
                client, settings, audio, args.save_replies, args.budget_wording
            )
        return {
            "kind": "gpu-conversation-smoke",
            "site": settings.site_id,
            "profile_digest": settings.profile_digest,
            "context_tokens": settings.context_tokens,
            "hardware_validated": False,
            "manual_review": "pending: listen to replies, compare spoken facts with cards and stock",
            **found,
            "status": "fail"
            if found["hard_failures"] or found["shopping_failures"]
            else "functional_checks_passed_manual_review_pending",
        }
    with Path(args.image).open("rb") as stream:
        image = sanitize_image(stream.read(MAX_IMAGE + 1))
    report = {
        "kind": "gpu-smoke",
        "site": settings.site_id,
        "profile_digest": settings.profile_digest,
        "hardware_validated": False,
        "manual_review": "pending: listen to speech, verify transcript/image feature and inspect peak VRAM/network",
        "turns": [],
    }
    async with httpx.AsyncClient(
        base_url=args.gateway,
        timeout=settings.turn_timeout_seconds + 30,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        info = await client.get("/v1/info")
        info.raise_for_status()
        if info.json().get("mode") != "nvidia" or info.json().get("site") != settings.site_id:
            raise ValueError("GPU smoke refuses mock or wrong-site gateway")
        for index in range(11):
            tenant = "a" if index == 10 or index % 2 == 0 else "b"
            sku_tenant = "b" if index == 10 else tenant
            setup = await client.post(
                "/v1/sessions",
                headers={
                    "Authorization": "Bearer " + os.environ[f"TENANT_{tenant.upper()}_KEY"],
                    "Idempotency-Key": uuid.uuid4().hex,
                },
                json={"capability_profile": "retail-voice-v1"},
            )
            setup.raise_for_status()
            session = setup.json()
            base = f"/v1/sessions/{session['session_id']}"
            headers = {"Authorization": "Bearer " + session["session_token"]}
            try:
                response = await client.post(
                    base + "/turns",
                    headers=headers | {"Idempotency-Key": uuid.uuid4().hex},
                    data={"sku": f"jacket-{sku_tenant}", "store_id": f"store-{sku_tenant}"},
                    files={
                        "audio": ("question.wav", audio, "audio/wav"),
                        "image": ("product.png", image, "image/png"),
                    },
                )
                response.raise_for_status()
                events = [json.loads(line) for line in response.text.splitlines() if line]
                if not events or any(
                    e.get("mode") != "nvidia" or e.get("profile_digest") != settings.profile_digest
                    for e in events
                ):
                    raise ValueError("Unexpected mode/profile in events")
                if index == 10:
                    if (
                        not any(e["event_type"] == "tool.denied" for e in events)
                        or events[-1]["event_type"] != "turn.failed"
                    ):
                        raise ValueError("Real-agent catalog denial not observed")
                    report["foreign_catalog_denied"] = True
                    continue
                if events[-1]["event_type"] != "turn.completed" or not any(
                    e["event_type"] == "tool.allowed" for e in events
                ):
                    raise ValueError("GPU turn failed")
                result = await client.get(events[-1]["result_path"], headers=headers)
                result.raise_for_status()
                data = result.json()
                if data.get("audio_kind") != "synthesized_speech" or data["tool_result"][
                    "navy_medium"
                ] != (3 if tenant == "a" else 7):
                    raise ValueError("Unexpected GPU result")
                reply = await client.get(data["audio_path"], headers=headers)
                reply.raise_for_status()
                validate_reply_audio(reply.content)
                report["turns"].append(
                    {
                        "trace_id": events[-1]["trace_id"],
                        "tenant": f"retailer-{tenant}",
                        "status": "complete",
                    }
                )
            finally:
                await client.delete(base, headers=headers)
    report["status"] = "functional_checks_passed_manual_review_pending"
    return report


# Expectations a run reports without failing the shopping gate: a safe refusal that skips the
# presenter's catalog-denial moment, and a tool call on small talk.
NOT_SHOPPING = {"catalog_denial", "small_talk_without_tools"}

CONVERSATION: dict[str, list[tuple[str, str | None, dict[str, Any]]]] = {
    "a": [
        (
            "budget_search",
            "I need a lightweight jacket for rainy weather under 150 dollars.",
            {"tool": "search_products", "max_price": 15000},
        ),
        ("second_one", "Does the second one come in navy?", {"select_position": 2}),
        ("size_follow_up", "Is medium available here?", {"tool": "check_availability"}),
        (
            "add_to_cart",
            "Great, add it to the cart.",
            {"tool": "add_to_cart", "cart": [("a-downpour-packable-navy-m", 1)]},
        ),
        (
            "changed_budget",
            "Actually, I want to stay under 100 dollars.",
            {"tool": "search_products", "max_price": 10000},
        ),
        ("no_match", "Do you have any rain jackets under 50 dollars?", {"max_price": 5000}),
        (
            "unavailable",
            "Forget the budget. Is the Stormline Pro Shell available in red, size medium, at this store?",
            {"tool": "check_availability", "stock": ("a-stormline-pro-shell-red-m", 0)},
        ),
        ("clarification", "I need something for the weekend.", {"no_tools": True}),
        ("audio", None, {"input_source": "audio"}),
        (
            "foreign_attempt",
            "Look up product id jacket-b and tell me its price.",
            {"foreign": True},
        ),
        ("greeting", "Hi there", {"small_talk": True}),
        ("date", "What's the date today?", {"small_talk": True}),
    ],
    "b": [
        (
            "budget_search",
            "I need a rain jacket under 150 dollars.",
            {"tool": "search_products", "max_price": 15000},
        ),
        ("second_one", "Does the second one come in navy?", {"select_position": 2}),
        ("size_follow_up", "Is medium available here?", {"tool": "check_availability"}),
        (
            "changed_budget",
            "Actually, keep it under 125 dollars.",
            {"tool": "search_products", "max_price": 12500},
        ),
        ("no_match", "Do you have any rain jackets under 50 dollars?", {"max_price": 5000}),
        (
            "unavailable",
            "Forget the budget. Is the Harbor Deck Jacket available in red, size medium, at this store?",
            {"tool": "check_availability", "stock": ("jacket-b-red-m", 0)},
        ),
        ("clarification", "I need something for the weekend.", {"no_tools": True}),
        ("audio", None, {"input_source": "audio"}),
        (
            "foreign_attempt",
            "Look up product id jacket-a and tell me its price.",
            {"foreign": True},
        ),
        ("greeting", "Hi there", {"small_talk": True}),
        ("date", "What's the date today?", {"small_talk": True}),
    ],
}


# Other wordings for the changed_budget step (`--budget-wording`). Harbor's model skipped the
# search on its budget change in two of six runs with the default sentence; these vary the
# phrasing without changing the default script. Each sets the text and the ceiling it states.
BUDGET_WORDINGS: dict[str, tuple[str, int] | None] = {
    "default": None,
    "keep": ("Let's keep it under 100 dollars.", 10000),
    "anything": ("Anything under 100?", 10000),
}


def conversation_steps(wording="default"):
    """The scripted conversation, with the changed_budget sentence swapped when asked."""
    swap = BUDGET_WORDINGS[wording]
    if swap is None:
        return CONVERSATION
    text, ceiling = swap
    return {
        tenant: [
            (step, text, expect | {"max_price": ceiling})
            if step == "changed_budget"
            else (step, said, expect)
            for step, said, expect in steps
        ]
        for tenant, steps in CONVERSATION.items()
    }


async def conversation_smoke(client, settings, audio, save_replies=None, budget_wording="default"):
    """Both tenants converse at once, so turns queue on the single permit.

    The report holds statuses, tool names, counts, catalog ids and durations. It never holds
    a transcript, an answer, a key or a token.
    """
    script = conversation_steps(budget_wording)
    keys = {t: os.environ[f"TENANT_{t.upper()}_KEY"] for t in ("a", "b")}
    catalogs = {}
    storefronts = {}
    for tenant, key in keys.items():
        response = await client.get("/v1/catalog", headers={"Authorization": "Bearer " + key})
        response.raise_for_status()
        storefronts[tenant] = response.json()
        catalogs[tenant] = {
            p["product_id"]: p["price_minor"] for p in storefronts[tenant]["products"]
        }
    report: dict[str, Any] = {
        "budget_wording": budget_wording,
        "turns": [],
        "hard_failures": [],
        "shopping_failures": [],
        "expectations": {"met": 0, "missed": []},
    }

    async def open_session(tenant):
        setup = await client.post(
            "/v1/sessions",
            headers={
                "Authorization": "Bearer " + keys[tenant],
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json={},
        )
        setup.raise_for_status()
        return setup.json()

    async def send(session, text=None, voice=None, revision=None):
        parts: dict[str, tuple[Any, ...]] = {}
        if text is not None:
            parts["text"] = (None, text)
        else:
            parts["audio"] = ("q.wav", voice, "audio/wav")
        if revision is not None:
            parts["display_revision"] = (None, str(revision))
        if session.get("selected_product_id"):
            parts["selected_product_id"] = (None, session["selected_product_id"])
        headers = {"Authorization": "Bearer " + session["session_token"]}
        response = await client.post(
            f"/v1/sessions/{session['session_id']}/messages",
            headers=headers | {"Idempotency-Key": uuid.uuid4().hex},
            files=parts,
        )
        if response.status_code != 200:
            return response.status_code, [], {}, b""
        events = [json.loads(line) for line in response.text.splitlines() if line]
        result = await client.get(events[-1]["result_path"], headers=headers)
        result.raise_for_status()
        body = result.json()
        reply = b""
        if body.get("audio_path"):
            fetched = await client.get(body["audio_path"], headers=headers)
            fetched.raise_for_status()
            reply = validate_reply_audio(fetched.content)
        return response.status_code, events, body, reply

    def check(tenant, step, condition, label, hard):
        if condition:
            if not hard:
                report["expectations"]["met"] += 1
            return
        if hard:
            report["hard_failures"].append(f"{tenant}:{step}:{label}")
        else:
            failure = f"{tenant}:{step}:{label}"
            report["expectations"]["missed"].append(failure)
            if label not in NOT_SHOPPING:
                # A safe model refusal may miss a presenter tool-denial demonstration, and a
                # greeting or date answer that calls a tool is reported here only. Product
                # discovery, selection and exact stock are shopping requirements.
                report["shopping_failures"].append(failure)

    async def run(tenant):
        session = await open_session(tenant)
        own, foreign = catalogs[tenant], catalogs["b" if tenant == "a" else "a"]
        cards_before: list[str] = []
        try:
            for step, text, expect in script[tenant]:
                revision = session.get("display_revision")
                status, events, body, reply = await send(
                    session, text, audio if text is None else None, revision
                )
                kinds = [e.get("event_type") for e in events]
                tools = [
                    {
                        "name": e["tool_name"],
                        "outcome": "completed" if e["event_type"] == "tool.completed" else "denied",
                        **({"result_count": e["result_count"]} if "result_count" in e else {}),
                        **({"reason": e["reason"]} if "reason" in e else {}),
                    }
                    for e in events
                    if e.get("event_type") in {"tool.completed", "tool.denied"}
                ]
                cards = [c["product_id"] for c in body.get("product_cards", [])]
                stage_ms: dict[str, int] = {}
                for e in events:
                    if e.get("event_type") in {"asr.completed", "model.completed", "tts.completed"}:
                        name = e["event_type"].split(".")[0]
                        stage_ms[name] = round(stage_ms.get(name, 0) + e["duration_ms"])
                record = {
                    "tenant": f"retailer-{tenant}",
                    "step": step,
                    "http_status": status,
                    "status": body.get("status"),
                    "reason": body.get("reason"),
                    "input_source": body.get("input_source"),
                    "trace_id": body.get("trace_id"),
                    "tools": tools,
                    "model_decisions": kinds.count("model.completed"),
                    "context_trimmed": "context.trimmed" in kinds,
                    "cards": cards,
                    "selected_product_id": body.get("selected_product_id"),
                    "display_revision": body.get("display_revision"),
                    "clarification": body.get("clarification"),
                    "stock_checks": [
                        {k: s[k] for k in ("product_id", "variant_id", "store_id", "quantity")}
                        for s in body.get("stock_checks", [])
                    ],
                    "queued_ms": body.get("queued_ms"),
                    "stage_ms": stage_ms,
                    "reply_bytes": len(reply),
                    "audio_kind": body.get("audio_kind"),
                }
                report["turns"].append(record)
                if save_replies and reply:
                    folder = Path(save_replies)
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / f"{tenant}-{step}.wav").write_bytes(reply)
                bad_events = [
                    e
                    for e in events
                    if e.get("mode") != "nvidia"
                    or e.get("profile_digest") != settings.profile_digest
                ]
                check(tenant, step, status == 200 and not bad_events, "events_nvidia_profile", True)
                check(tenant, step, body.get("status") == "complete", "turn_complete", True)
                check(
                    tenant, step, set(cards) <= set(own) and len(cards) <= 3, "cards_in_scope", True
                )
                check(tenant, step, not set(cards) & set(foreign), "no_foreign_cards", True)
                if body.get("audio_path"):
                    check(
                        tenant, step, body.get("audio_kind") == "synthesized_speech", "speech", True
                    )
                stock = body.get("stock_checks", [])
                if stock or (
                    STOCK_CLAIM.search(body.get("answer", "")) and not body.get("clarification")
                ):
                    verified = len(stock) == 1 and body.get("answer") == stock_answer(stock[0])
                    check(tenant, step, verified, "stock_answer_verified", False)
                if "max_price" in expect:
                    searched = any(
                        t["name"] == "search_products" and t["outcome"] == "completed"
                        for t in tools
                    )
                    # Cards from a search in this turn must respect the stated budget. Whether
                    # the model searched at all is model behavior, reported as an expectation.
                    check(tenant, step, searched, "searched_this_turn", False)
                    if searched:
                        within = all(own[c] <= expect["max_price"] for c in cards)
                        check(tenant, step, within, "cards_within_budget", True)
                names = [t["name"] for t in tools if t["outcome"] == "completed"]
                if step != "add_to_cart" and "add_to_cart" in names:
                    # Only the add step may change the cart. Recorded only when it
                    # happens, so the met count stays comparable with earlier runs.
                    check(tenant, step, False, "cart_unchanged", False)
                if "tool" in expect:
                    check(tenant, step, expect["tool"] in names, f"used_{expect['tool']}", False)
                if step == "second_one":
                    check(
                        tenant,
                        step,
                        body.get("selected_variant_id") is None and not body.get("stock_checks"),
                        "color_does_not_choose_size",
                        False,
                    )
                if "select_position" in expect:
                    position = expect["select_position"]
                    if len(cards_before) >= position:
                        wanted = cards_before[position - 1]
                        check(
                            tenant,
                            step,
                            body.get("selected_product_id") == wanted,
                            "selected",
                            False,
                        )
                    else:
                        # Fewer cards than the spoken position (Harbor has one rain jacket under
                        # the budget): the right outcome is the fixed clarification, no model run.
                        check(
                            tenant,
                            step,
                            body.get("clarification") is True and not tools,
                            "position_clarified",
                            False,
                        )
                if "stock" in expect:
                    variant, quantity = expect["stock"]
                    product = next(
                        p["product_id"]
                        for p in storefronts[tenant]["products"]
                        if any(v["variant_id"] == variant for v in p["variants"])
                    )
                    expected = (product, variant, storefronts[tenant]["default_store_id"], quantity)
                    seen = [
                        tuple(
                            s.get(k) for k in ("product_id", "variant_id", "store_id", "quantity")
                        )
                        for s in body.get("stock_checks", [])
                    ]
                    check(tenant, step, seen == [expected], "stock_fact", False)
                    check(
                        tenant,
                        step,
                        body.get("selected_product_id") == product,
                        "stock_selection",
                        False,
                    )
                if "cart" in expect:
                    # The committed cart after this turn: exact variants and quantities.
                    seen = [(i.get("variant_id"), i.get("quantity")) for i in body.get("cart", [])]
                    expected_cart = [tuple(item) for item in expect["cart"]]
                    check(tenant, step, seen == expected_cart, "cart_contents", False)
                if expect.get("no_tools"):
                    check(tenant, step, not tools, "clarified_without_tools", False)
                if expect.get("small_talk"):
                    check(tenant, step, not tools, "small_talk_without_tools", False)
                if "input_source" in expect:
                    check(
                        tenant,
                        step,
                        body.get("input_source") == expect["input_source"],
                        "audio_input",
                        True,
                    )
                if expect.get("foreign"):
                    record["foreign_outcome"] = (
                        "denied_by_catalog"
                        if any(t["outcome"] == "denied" for t in tools)
                        else "model_made_no_tool_call"
                        if not tools
                        else "other_tool_result"
                    )
                    check(
                        tenant,
                        step,
                        record["foreign_outcome"] == "denied_by_catalog",
                        "catalog_denial",
                        False,
                    )
                session["display_revision"] = body.get("display_revision", revision)
                session["selected_product_id"] = body.get("selected_product_id")
                cards_before = cards
        finally:
            await client.delete(
                f"/v1/sessions/{session['session_id']}",
                headers={"Authorization": "Bearer " + session["session_token"]},
            )

    await asyncio.gather(run("a"), run("b"))
    # A clean sign-in after sign-out still works and stays in scope.
    fresh = await open_session("a")
    try:
        status, events, body, _ = await send(fresh, script["a"][0][1])
        cards = [c["product_id"] for c in body.get("product_cards", [])]
        report["fresh_session"] = {"status": body.get("status"), "cards": cards}
        check("a", "fresh_session", body.get("status") == "complete", "turn_complete", True)
        check("a", "fresh_session", set(cards) <= set(catalogs["a"]), "cards_in_scope", True)
    finally:
        await client.delete(
            f"/v1/sessions/{fresh['session_id']}",
            headers={"Authorization": "Bearer " + fresh["session_token"]},
        )
    report["queued_turns"] = sum(1 for t in report["turns"] if t["queued_ms"])
    report["runtime_checks_passed"] = not report["hard_failures"]
    report["shopping_checks_passed"] = not (report["hard_failures"] or report["shopping_failures"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["preflight", "serve-vlm", "download-models", "smoke-gpu"]
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Actually start the model container; otherwise print its command",
    )
    parser.add_argument("--accept-model-licenses", action="store_true")
    parser.add_argument("--allow-inference", action="store_true")
    parser.add_argument("--gateway", default="http://127.0.0.1:8765")
    parser.add_argument("--audio")
    parser.add_argument("--image")
    parser.add_argument("--output")
    parser.add_argument(
        "--conversation",
        action="store_true",
        help="smoke-gpu: scripted shopping conversations for both tenants at once",
    )
    parser.add_argument("--save-replies", help="smoke-gpu --conversation: folder for reply WAVs")
    parser.add_argument(
        "--budget-wording",
        choices=sorted(BUDGET_WORDINGS),
        default="default",
        help="smoke-gpu --conversation: the changed_budget sentence (default keeps the script)",
    )
    args = parser.parse_args()
    try:
        settings = GPUSettings.from_env()
        if args.command == "preflight":
            report = asyncio.run(preflight(settings))
        elif args.command == "download-models":
            report = download(settings, args.accept_model_licenses)
        elif args.command == "serve-vlm":
            command = vlm_command(settings)
            if args.start:
                artifact_check(settings)
                cache = settings.vlm_cache_dir
                if cache.is_symlink():
                    raise ValueError("Refusing a symlink for the model compiler cache")
                cache.mkdir(parents=True, exist_ok=True, mode=0o700)
                cache.chmod(0o700)
                subprocess.run(command, check=True)
            else:
                print(shlex.join(command))
            return 0
        else:
            report = asyncio.run(smoke(settings, args))
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 1 if report.get("status") == "fail" else 0
    except Exception as exc:
        # No request bodies, tokens, transcripts, backend URLs or raw SDK errors in a shared receipt.
        print(
            json.dumps(
                {
                    "status": "fail",
                    "error_type": type(exc).__name__,
                    "hint": "Check GPU_CONFIG, pinned artifacts, local services and the one-GPU runbook.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
