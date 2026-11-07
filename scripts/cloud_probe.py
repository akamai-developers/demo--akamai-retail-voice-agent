"""Run readiness or synthetic shopping checks on this site's GPU host."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from demo import LOCAL, ROOT, private_write, read_env

sys.path.insert(0, str(ROOT / "tools"))
import httpx
from measure_latency import trial


async def check(client, values):
    info = (await client.get("/v1/info")).json()
    if info.get("mode") != "nvidia" or info.get("fixture_enabled"):
        raise ValueError("Expected the real GPU runtime")
    for key in (None, "invalid-demo-key"):
        headers = {"Authorization": "Bearer " + key} if key else {}
        if (await client.get("/v1/catalog", headers=headers)).status_code != 401:
            raise ValueError("Unauthenticated catalog access was not rejected")
    catalogs = []
    for name in ("TENANT_A_KEY", "TENANT_B_KEY"):
        response = await client.get(
            "/v1/catalog", headers={"Authorization": "Bearer " + values[name]}
        )
        response.raise_for_status()
        catalogs.append({r["product_id"] for r in response.json()["products"]})
    if not all(catalogs) or catalogs[0] & catalogs[1]:
        raise ValueError("Retailer catalogs are not isolated")
    if values.get("TRYON_MODE") == "live":
        response = await client.get("http://127.0.0.1:8083/health")
        response.raise_for_status()
        if response.json().get("ready") is not True or info.get("tryon", {}).get("mode") != "live":
            raise ValueError("The live preview worker is not ready")
    return {
        "mode": info["mode"],
        "site": info["site"],
        "authentication": "passed",
        "catalog_isolation": "passed",
    }


async def exercise(command, values):
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8765", timeout=240, trust_env=False
    ) as client:
        report = await check(client, values)
        if command != "check":
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8082", timeout=240, trust_env=False
            ) as speech:
                response = await speech.post(
                    "/tts",
                    json={
                        "text": "I am looking for a raincoat under one hundred and fifty dollars."
                    },
                )
                response.raise_for_status()
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "source.wav", Path(directory) / "voice.wav"
                source.write_bytes(response.content)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source),
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        str(target),
                    ],
                    check=True,
                )
                steps = [("voice", None)]
                turns = await trial(
                    client, values["TENANT_A_KEY"], target.read_bytes(), 1, "inline", steps=steps
                )
            if any(
                r.get("status") != "complete"
                or not r.get("reply_audio_bytes")
                or "asr_ms" not in r.get("gateway", {})
                or not r["gateway"]["decisions"]
                for r in turns
            ):
                raise ValueError("Real speech and model warmup did not complete")
            report["voice"] = turns
        if command == "smoke":
            await shopping(client, values, report)
        report["passed"] = True
        private_write(
            LOCAL / "verification" / f"cloud-{command}.json", json.dumps(report, indent=2) + "\n"
        )
        print(
            f"{command}: passed on {report['site']}. Receipt: .local/verification/cloud-{command}.json"
        )


async def shopping(client, values, report):
    import uuid

    from measure_latency import turn

    response = await client.post(
        "/v1/sessions",
        headers={
            "Authorization": "Bearer " + values["TENANT_A_KEY"],
            "Idempotency-Key": uuid.uuid4().hex,
        },
        json={},
    )
    response.raise_for_status()
    session = response.json()
    steps = [
        "Show me the Drizzle Anorak in navy medium. Let me try it on.",
        "Looks good. Add it to my cart.",
        "Try the Downpour Packable Shell in yellow medium.",
    ]
    records = []
    saved_cart = None
    try:
        for index, text in enumerate(steps):
            record, body, audio = await turn(client, session, text=text, audio_fetch="inline")
            if body.get("status") != "complete" or not audio:
                raise ValueError("A shopping turn did not complete")
            records.append(record)
            session.update(
                display_revision=body.get("display_revision"),
                selected_product_id=body.get("selected_product_id"),
            )
            if index == 0:
                if (
                    body.get("preview", {}).get("kind") != "live"
                    or body.get("selected_variant_id") != "a-drizzle-anorak-navy-m"
                ):
                    raise ValueError("Expected a live navy medium preview")
                image = await client.get(
                    body["preview"]["image_path"],
                    headers={"Authorization": "Bearer " + session["session_token"]},
                )
                image.raise_for_status()
                report["preview_sha256"] = hashlib.sha256(image.content).hexdigest()
            elif index == 1:
                if (
                    body.get("preview")
                    or len(body["cart"]) != 1
                    or body["cart"][0]["variant_id"] != "a-drizzle-anorak-navy-m"
                    or body["cart"][0]["quantity"] != 1
                ):
                    raise ValueError("Preview-to-cart continuity failed")
                saved_cart = body["cart"]
            elif (
                body.get("preview")
                or "out of stock" not in body["answer"].lower()
                or body["cart"] != saved_cart
            ):
                raise ValueError("Sold-out variant was not blocked")
    finally:
        await client.delete(
            "/v1/sessions/" + session["session_id"],
            headers={"Authorization": "Bearer " + session["session_token"]},
        )
    report["shopping"] = records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "warmup", "smoke"])
    args = parser.parse_args()
    values = read_env()
    if values.get("DEMO_MODE") != "nvidia":
        raise ValueError("Run this check on the deployed GPU host")
    subprocess.run(
        [sys.executable, "-m", "retail_voice.cli", "preflight"], env=os.environ | values, check=True
    )
    asyncio.run(exercise(args.command, values))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, httpx.HTTPError) as error:
        raise SystemExit(str(error)) from None
