"""Chromium end-to-end check of the shopper storefront.

Default: starts an isolated loopback mock server and runs every step, including the mock-only
fixture. With BASE_URL set (for example an SSH tunnel to the GPU gateway) it uses that gateway
and TENANT_A_KEY / TENANT_B_KEY from the environment, skips mock-only steps and allows longer
model turns. BASE_URL requires FAKE_MIC_WAV to name a real speech recording on this workstation.
A fake microphone plays it once (a generated tone only for the isolated mock) through the
real AudioWorklet capture path, then silence. Nobody presses Stop: the check waits about
FAKE_MIC_SECONDS (default 2.5, the length of the speech) plus the 0.8 s silence hangover for the
recording to stop on its own. Screenshots go to EVIDENCE_DIR (default artifacts/browser).
"""

import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.getenv("EVIDENCE_DIR", str(ROOT / "artifacts/browser")))
RAIN = "I need a lightweight jacket for rainy weather under 150 dollars"
# Test-side observers. They record what the page showed; the app has no test hooks.
RECORD_STAGES = """() => {
  window.recordedStages = [];
  window.recordedLevels = [];
  const line = document.getElementById('state-line');
  const meter = document.getElementById('mic-level');
  // Each textContent write adds a new text node, so added nodes keep every text, even ones
  // replaced in the same task (mock events arrive in one chunk).
  window.recordedTimes = [];
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    window.recordedStages.push(node.textContent);
    window.recordedTimes.push(performance.now());
  }))).observe(line, { childList: true });
  new MutationObserver(() => window.recordedLevels.push(Number(meter.getAttribute('aria-valuenow'))))
    .observe(meter, { attributes: true, attributeFilter: ['aria-valuenow'] });
}"""


def in_order(seen, expected):
    """Each expected text appears in `seen` after the previous one."""
    at = -1
    for text in expected:
        later = [i for i, item in enumerate(seen) if i > at and item == text]
        assert later, (text, seen)
        at = later[0]


def microphone_settings(env):
    """Reject unusable microphone configuration before any gateway or browser work."""
    mic = env.get("FAKE_MIC_WAV")
    if env.get("BASE_URL") and not mic:
        raise ValueError(
            "BASE_URL runs require FAKE_MIC_WAV pointing to a local real-speech WAV. "
            "Use FAKE_MIC_SECONDS=5 for shopping-rain.wav; a tone is only for mock mode."
        )
    seconds = float(env.get("FAKE_MIC_SECONDS", "2.5"))
    if not math.isfinite(seconds) or not 0 < seconds <= 20:
        raise ValueError("FAKE_MIC_SECONDS must be greater than 0 and at most 20")
    if mic:
        path = Path(mic).expanduser().resolve(strict=True)
        with wave.open(str(path), "rb") as wav:
            if wav.getnframes() == 0:
                raise ValueError("FAKE_MIC_WAV must contain audio frames")
        mic = str(path)
    return mic, seconds


def tone_wav(path):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(i * 2 * math.pi * 440 / 16000)))
                for i in range(16000 * 3)
            )
        )


# Digital silence after the fake speech. Chromium's fake device did not reliably produce silence
# after a file ended (the level stayed near the speech threshold), and every reopened microphone
# plays the file again from the start. Trailing zeros make "speech, then quiet" deterministic.
TRAILING_SILENCE_SECONDS = 4


def padded_copy(source, dest, seconds=TRAILING_SILENCE_SECONDS):
    """Copy a WAV and append digital silence, for the fake microphone."""
    with wave.open(str(source), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(wav.getnframes())
    zero = b"\x80" if params.sampwidth == 1 else b"\x00"  # 8-bit WAV is unsigned
    silence = zero * (int(params.framerate * seconds) * params.sampwidth * params.nchannels)
    with wave.open(str(dest), "wb") as out:
        out.setparams(params)
        out.writeframes(frames + silence)


NOT_RECORDED = "Not recorded yet. Recorded footage will show the build and date it was made from."


def make_walkthrough(folder):
    """A six-second test walkthrough in a temp folder, or None without ffmpeg. VP9 and Opus in
    MP4: Playwright's Chromium has no H.264 decoder. Generated per run, never committed."""
    if not shutil.which("ffmpeg"):
        return None
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:d=6:r=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libvpx-vp9", "-b:v", "100k", "-c:a", "libopus", "-shortest",
            "-movflags", "+faststart", str(root / "walkthrough.mp4"),
        ],
        check=True,
    )  # fmt: skip
    (root / "walkthrough.vtt").write_text(
        "WEBVTT\n\n00:00.000 --> 00:02.500\nTest caption one.\n\n"
        "00:03.000 --> 00:05.500\nTest caption two.\n"
    )
    manifest = {
        "schema_version": 1,
        "build": "test0001",
        "recorded": "2026-11-02",
        "captions": "walkthrough.vtt",
        "chapters": [{"start": 0, "title": "First test chapter"}, {"start": 3, "title": "Second"}],
    }
    (root / "walkthrough.json").write_text(json.dumps(manifest))
    return root


def start_server(env):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    logs = tempfile.TemporaryFile()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "retail_voice.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=logs,
        stderr=logs,
    )
    origin = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urlopen(origin + "/livez", timeout=0.2).close()
            return server, logs, origin
        except OSError:
            time.sleep(0.05)
    server.terminate()
    raise RuntimeError("test server failed to start")


def check_walkthrough(browser, env, checks):
    """The Video walkthrough tab with a recording: labeled recorded, no autoplay, captions on,
    chapters seek and play, keyboard works, and nothing scrolls sideways."""
    from playwright.sync_api import expect

    server, logs, origin = start_server(env)
    context = browser.new_context(viewport={"width": 1512, "height": 982}, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(origin)
        page.locator("#how-toggle").click()
        page.locator("#how-tab-video").click()
        player = page.locator("#walkthrough-player")
        expect(player).to_be_visible()
        expect(page.locator("#walkthrough-status")).to_be_hidden()
        expect(page.locator(".recorded-label")).to_have_text("RECORDED")
        expect(page.locator("#walkthrough-recorded")).to_contain_text("not live activity")
        expect(page.locator("#walkthrough-meta")).to_have_text("on 2026-11-02 from build test0001")
        video = page.locator("#walkthrough-video")
        page.wait_for_timeout(500)
        assert video.evaluate("v => v.paused && v.currentTime === 0"), "no autoplay"
        assert video.evaluate("v => v.textTracks[0].mode") == "showing", "captions on"
        expect(page.locator("#walkthrough-transcript")).to_contain_text("Test caption two.")
        chapters = page.locator("#walkthrough-chapters .chapter")
        expect(chapters).to_have_count(2)
        chapters.nth(1).click()
        waited = 0
        while video.evaluate("v => v.paused || v.currentTime < 3.2") and waited < 5000:
            page.wait_for_timeout(100)
            waited += 100
        assert video.evaluate("v => !v.error && v.currentTime >= 3"), "plays from chapter 2"
        expect(chapters.nth(1)).to_have_attribute("aria-current", "true")
        video.evaluate("v => v.pause()")
        chapters.nth(0).focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        assert video.evaluate("v => v.currentTime < 1"), "Enter on chapter 1 seeks back"
        video.evaluate("v => v.pause()")
        for width, height in [(1512, 982), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#how").screenshot(path=str(OUTPUT / f"walkthrough-{width}x{height}.png"))
        assert not errors, errors
        checks.append("recorded walkthrough: labeled, no autoplay, captions on, chapters play")
    finally:
        context.close()
        server.terminate()
        server.wait(timeout=10)
        logs.close()


def main():
    base = os.getenv("BASE_URL")
    mic, mic_seconds = microphone_settings(os.environ)
    from playwright.sync_api import expect, sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)
    mock = not base
    turn = 20_000 if mock else 150_000
    # The fake speech plays for about FAKE_MIC_SECONDS; the gate then waits 0.8 s of quiet.
    auto_stop_ms = int((mic_seconds + 0.8 + 6) * 1000)
    checks = []
    measured = {}
    server = logs = None
    if mock:
        env = os.environ | {
            "DEMO_MODE": "mock",
            "TENANT_A_KEY": "browser-tenant-a-fixture",
            "TENANT_B_KEY": "browser-tenant-b-fixture",
            "OPERATOR_KEY": "browser-operator-fixture",
        }
        server, logs, origin = start_server(env)
    else:
        env = os.environ
        origin = base.rstrip("/")
    keys = {"a": env["TENANT_A_KEY"], "b": env["TENANT_B_KEY"]}
    temp = tempfile.TemporaryDirectory()
    if not mic:
        mic = str(Path(temp.name) / "fake-mic.wav")
        tone_wav(mic)
    fake_mic = str(Path(temp.name) / "fake-mic-then-silence.wav")
    padded_copy(mic, fake_mic)
    try:
        with sync_playwright() as playwright:
            launch = {
                "headless": True,
                "args": [
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-audio-capture={fake_mic}%noloop",
                ],
            }
            if os.getenv("CHROMIUM_PATH"):
                launch["executable_path"] = os.environ["CHROMIUM_PATH"]
            browser = playwright.chromium.launch(**launch)
            context = browser.new_context(
                viewport={"width": 1512, "height": 982}, reduced_motion="reduce"
            )
            context.grant_permissions(["microphone"], origin=origin)
            page = context.new_page()
            errors, external = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: external.append(request.url)
                if not request.url.startswith(origin) and not request.url.startswith("blob:")
                else None,
            )

            def catalog_names(tenant):
                response = page.request.get(
                    origin + "/v1/catalog", headers={"Authorization": "Bearer " + keys[tenant]}
                )
                assert response.ok
                return [p["name"] for p in response.json()["products"]]

            def card_names():
                return page.locator("#cards .card .name").all_text_contents()

            def entries():
                return page.locator("#transcript li")

            def sign_in(tenant):
                page.locator("#key").fill(keys[tenant])
                page.locator("#signin-button").click()
                expect(page.locator("#store")).to_be_visible()
                assert page.locator("#key").input_value() == ""

            def played_timings(timeout_ms):
                """window.__timings once the latest reply has started playing. Polled with
                evaluate: the page's CSP blocks string wait_for_function."""
                waited = 0
                while waited < timeout_ms:
                    found = page.evaluate("window.__timings")
                    if found and found["marks"].get("playing") is not None:
                        return found
                    page.wait_for_timeout(100)
                    waited += 100
                raise AssertionError(
                    ("reply never started playing", page.evaluate("window.__timings"))
                )

            def send(text):
                """Send a typed message and wait for its two transcript entries."""
                before = entries().count()
                page.locator("#text").fill(text)
                page.locator("#send").click()
                expect(entries()).to_have_count(before + 2, timeout=turn)

            try:
                page.goto(origin)
                expect(page.locator("#signin-button")).to_be_enabled()
                badge = "LOCAL · MOCK" if mock else "NVIDIA · SINGLE SITE"
                expect(page.locator("#mode-badge")).to_have_text(badge)

                # How it works reads before sign-in: the diagram, each box's details and the
                # two tabs, all by keyboard. The live parts wait for a signed-in turn.
                how = page.locator("#how")
                page.locator("#how-toggle").click()
                expect(how).to_be_visible()
                expect(page.locator("#platform-diagram")).to_be_visible()
                expect(page.locator("#how-live-note")).to_have_text(
                    "Sign in and send a message to see a live turn."
                )
                expect(page.locator("#how-timings")).to_be_hidden()
                page.locator("#how-tab-arch").focus()
                page.keyboard.press("ArrowRight")
                expect(page.locator("#how-tab-live")).to_be_focused()
                expect(page.locator("#arch")).to_be_visible()
                page.keyboard.press("ArrowRight")
                expect(page.locator("#how-tab-video")).to_be_focused()
                expect(page.locator("#how-panel-video")).to_be_visible()
                expect(page.locator("#how-panel-arch")).to_be_hidden()
                # Without a recording the tab shows the chapter plan and says so.
                if not page.request.get(origin + "/v1/info").json().get("walkthrough"):
                    expect(page.locator("#walkthrough-status")).to_have_text(NOT_RECORDED)
                    expect(page.locator("#walkthrough-chapters li")).to_have_count(5)
                    expect(page.locator("#walkthrough-player")).to_be_hidden()
                    expect(page.locator("#walkthrough-transcript")).to_contain_text(
                        "available with the recording"
                    )
                page.keyboard.press("ArrowLeft")
                expect(page.locator("#how-tab-live")).to_be_focused()
                expect(page.locator("#how-panel-live")).to_be_visible()
                page.keyboard.press("Tab")
                expect(page.locator("#arch-node-mic")).to_be_focused()
                ring = page.locator("#arch-node-mic .ring").evaluate(
                    "e => getComputedStyle(e).stroke"
                )
                assert ring not in ("none", "transparent", "rgba(0, 0, 0, 0)"), ring
                page.keyboard.press("Tab")
                page.keyboard.press("Enter")
                expect(page.locator("#arch-detail-title")).to_have_text("Gateway and admission")
                page.locator("#arch-node-tts").click()
                expect(page.locator("#arch-detail-title")).to_have_text("Magpie speech synthesis")
                expect(page.locator("#arch-detail-facts")).to_contain_text(
                    "mock-tts" if mock else "magpie"
                )
                for width, height in [(1512, 982), (390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    how.screenshot(path=str(OUTPUT / f"how-before-signin-{width}x{height}.png"))
                page.set_viewport_size({"width": 1512, "height": 982})
                page.locator("#how-toggle").click()
                expect(how).to_be_hidden()
                checks.append("how it works reads before sign-in: diagram, details, keyboard tabs")

                # Retailer A: the storefront matches the scoped catalog and reserves nothing.
                sign_in("a")
                a_names = catalog_names("a")
                expect(page.locator("#cards .card")).to_have_count(len(a_names))
                assert card_names() == a_names
                expect(page.locator("#cards img").first).to_be_visible()
                checks.append("storefront cards match /v1/catalog")

                # First message: lose the response once, then retry with the same key and body.
                keys_seen, bodies = [], []

                def lose_message_once(route):
                    keys_seen.append(route.request.headers["idempotency-key"])
                    response = route.fetch()
                    bodies.append(response.text())
                    if len(keys_seen) == 1:
                        route.abort(
                            "failed"
                        )  # The server ran the turn; the browser lost the reply.
                    else:
                        route.fulfill(response=response)

                page.route("**/messages", lose_message_once)
                page.locator("#text").fill(RAIN)
                page.locator("#send").click()
                expect(page.locator("#retry")).to_be_visible(timeout=turn)
                expect(page.locator("#state-line")).to_contain_text("Connection lost")
                assert page.locator("#text").input_value() == RAIN
                page.locator("#retry").click()
                expect(entries()).to_have_count(2, timeout=turn)
                assert keys_seen[0] == keys_seen[1]
                if mock:
                    assert bodies[0] == bodies[1]
                page.unroute("**/messages")
                checks.append("lost message response retried with the same key")
                result_names = card_names()
                assert 1 <= len(result_names) <= 3 and set(result_names) <= set(a_names)
                expect(page.locator("#products-label")).to_contain_text("RESULTS")
                expect(entries().nth(1)).to_contain_text("FIXTURE REPLY" if mock else "ASSISTANT")
                if mock:
                    assert result_names == [
                        "Drizzle Anorak",
                        "Downpour Packable Shell",
                        "Ridgeline Rain Shell",
                    ]
                # The reply WAV downloads after the transcript renders; a long reply takes a moment.
                expect(page.locator("#audio")).to_have_attribute(
                    "src", re.compile(r"^blob:"), timeout=turn
                )
                checks.append("text message updates cards, transcript and playback source")

                # Browser timings: marks on the page's clock in order, a read-only view.
                typed = played_timings(turn)
                order = ["request", "firstByte", "terminal", "result", "audio", "playing"]
                marks = typed["marks"]
                assert all(isinstance(marks.get(m), (int, float)) for m in order), marks
                assert all(marks[a] <= marks[b] for a, b in zip(order, order[1:])) or (
                    marks["audio"] < marks["result"] and typed["audio_fetch"] == "with the result"
                ), marks
                assert typed["input_source"] == "text" and typed["capture"] is None, typed
                assert typed["audio_fetch"] == "with the result", typed
                assert typed["audio_bytes"] > 44, typed
                assert (
                    page.evaluate(
                        "() => { try { window.__timings = 1 } catch (e) {} return typeof window.__timings }"
                    )
                    == "object"
                )
                expect(page.locator("#how-steps")).to_contain_text(
                    "Browser wait from request start to playback start"
                )
                measured["typed_search_request_to_playback_ms"] = round(
                    typed["durations"]["request_to_playing_ms"]
                )
                measured["typed_search_reply_audio_bytes"] = typed["audio_bytes"]
                checks.append("browser timings: ordered marks, read-only window.__timings")

                for width, height in [(1920, 1080), (1512, 982), (1366, 1024), (390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("document.fonts.ready")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        width
                    )
                    page.screenshot(
                        path=str(OUTPUT / f"store-{width}x{height}.png"), full_page=True
                    )
                page.set_viewport_size({"width": 1512, "height": 982})
                checks.append("four viewports without horizontal scroll, screenshots saved")

                # "The second one" resolves to the second card on screen.
                second = result_names[1] if len(result_names) > 1 else None
                send("Does the second one come in navy?")
                if second:
                    expect(page.locator('#cards .card[aria-pressed="true"] .name')).to_have_text(
                        second
                    )
                if mock:
                    expect(entries().last).to_contain_text("Downpour Packable Shell comes in navy")
                checks.append("follow-up resolves the second card")

                # Product focus: the positional follow-up brings that product forward.
                # Back collapses it, the numbered cards keep their order, a click reopens it.
                focus = page.locator("#focus")
                positions = page.locator("#cards .card .position")
                if second:
                    expect(focus).to_be_visible()
                    expect(page.locator("#focus-name")).to_have_text(second)
                    page.locator("#focus-back").click()
                    expect(focus).to_be_hidden()
                    numbers = positions.all_text_contents()
                    assert numbers == [str(i + 1) for i in range(len(result_names))], numbers
                    page.locator("#cards .card").nth(1).click()
                    expect(focus).to_be_visible()
                    expect(page.locator("#focus-name")).to_have_text(second)
                    expect(page.locator("#focus-name")).to_be_focused()
                    expect(page.locator("#focus-photo img")).to_be_visible()
                    photo = page.locator("#focus-photo").bounding_box()
                    tile = page.locator("#cards .card").nth(1).bounding_box()
                    assert photo["width"] > 1.5 * tile["width"], (photo, tile)
                    assert positions.all_text_contents() == numbers
                    if mock:
                        expect(page.locator("#focus-tags")).to_contain_text("packable")
                    for width, height in [(1512, 982), (390, 844)]:
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(
                            path=str(OUTPUT / f"focus-{width}x{height}.png"), full_page=True
                        )
                    page.set_viewport_size({"width": 1512, "height": 982})
                    page.keyboard.press("Escape")
                    expect(focus).to_be_hidden()
                    expect(page.locator(".card[aria-pressed='true']")).to_be_focused()
                    checks.append(
                        "focus panel opens, shows the large photo, Back and Escape close it"
                    )
                # Both backends must settle the size before a cart addition. A color-only
                # follow-up cannot authorize a model-guessed variant.
                send("Is medium available here?")
                if second:
                    expect(focus).to_be_visible()
                    expect(page.locator("#focus-name")).to_have_text(second)
                    expect(page.locator("#stock-line")).to_be_visible()
                if mock:
                    expect(page.locator("#stock-line")).to_contain_text("navy M: 4 in stock")
                    expect(page.locator("#focus-photo img")).to_have_attribute(
                        "alt", re.compile(r"\bin navy\b")
                    )
                    page.screenshot(path=str(OUTPUT / "focus-stock-1512x982.png"), full_page=True)
                    checks.append("verified stock opens the focus panel with its stock line")
                    # a turn without a stock check keeps the checked color's photo.
                    send("What's the date today?")
                    expect(focus).to_be_visible()
                    expect(page.locator("#focus-photo img")).to_have_attribute(
                        "alt", re.compile(r"\bin navy\b")
                    )
                    expect(page.locator("#stock-line")).to_be_hidden()
                    page.locator("#focus-back").click()
                    expect(focus).to_be_hidden()
                    checks.append("the focus photo keeps the checked color after small talk")

                # Only a completed addition can populate the cart. A denied tool call is
                # visible in diagnostics too, but does not mean anything was added.
                cart = page.locator("#cart")
                expect(cart).to_be_hidden()
                send("Great, add it to the cart.")
                added = any(
                    event.get("event_type") == "tool.completed"
                    and event.get("tool_name") == "add_to_cart"
                    for event in json.loads(page.locator("#raw").text_content())
                )
                assert added, "the settled in-stock item must be added"
                if added:
                    expect(cart).to_be_visible()
                    expect(page.locator("#cart-count")).to_have_text("1 ITEM")
                    expect(page.locator("#cart-items")).to_contain_text(second or "")
                    expect(page.locator("#cart-total")).to_contain_text("Total $")
                    checks.append("add to cart shows the committed cart")
                else:
                    measured["cart_add_not_called"] = 1

                # How it works shows the observed steps of that turn: marks on the diagram, one
                # current box, and the timings table.
                page.locator("#how-toggle").click()
                expect(page.locator("#how")).to_be_visible()
                page.locator("#how-tab-live").click()
                expect(page.locator("#how-steps")).to_contain_text("Model decision")
                expect(page.locator("#how-steps")).to_contain_text(
                    "skipped: gateway handled the request"
                )
                expect(page.locator("#how-steps")).to_contain_text("skipped: typed message")
                expect(page.locator("#how-steps")).to_contain_text("Gateway turn total")
                expect(page.locator("#arch-node-result.done")).to_have_count(1)
                expect(page.locator("#arch-node-asr.skipped")).to_have_count(1)
                expect(page.locator("#arch-node-llm.skipped")).to_have_count(1)
                expect(page.locator("#arch .node.current")).to_have_count(1)
                expect(page.locator("#how-live-note")).to_contain_text("typed message, complete")
                for width, height in [(1512, 982), (390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.locator("#how").screenshot(path=str(OUTPUT / f"how-{width}x{height}.png"))
                page.set_viewport_size({"width": 1512, "height": 982})
                checks.append("how it works marks the observed stages and lists their timings")

                expect(page.locator("#store")).to_be_hidden()
                page.locator("#how-tab-arch").click()
                expect(page.locator("#platform-diagram")).to_be_visible()
                expect(page.locator("#request-stage")).to_contain_text("Functions*")
                page.locator("#how-toggle").click()
                expect(page.locator("#store")).to_be_visible()
                expect(page.locator("#how")).to_be_hidden()

                # Small talk: a greeting, the date and the weather need no catalog lookup
                # and leave the cards and the selection alone. On the GPU this is model
                # behavior, so it is counted, not asserted.
                cards_before = card_names()
                chosen = page.locator('#cards .card[aria-pressed="true"] .name')
                chosen_before = chosen.all_text_contents()
                for text, fixture in [
                    ("Hi there", "hello, welcome to Northline Downtown."),
                    ("What's the date today?", "at the store (America/Denver)."),
                    ("What's the weather like?", "I cannot check the weather here."),
                ]:
                    send(text)
                    looked_up = "Catalog tool" in (page.locator("#how-steps").text_content() or "")
                    if mock:
                        expect(entries().last).to_contain_text(fixture)
                        assert not looked_up, text
                        assert card_names() == cards_before, text
                        assert chosen.all_text_contents() == chosen_before, text
                    elif looked_up:
                        measured["small_talk_catalog_lookups"] = (
                            measured.get("small_talk_catalog_lookups", 0) + 1
                        )
                checks.append("small talk answers without the catalog and keeps the cards")

                # Another retailer's product id is denied by the catalog, not shown.
                send("Look up product id jacket-b please")
                b_names = catalog_names("b")
                assert not set(card_names()) & set(b_names)
                if mock:
                    assert "Catalog refused" in page.locator("#how-steps").text_content()
                    expect(entries().last).to_contain_text("could not find")
                checks.append("foreign product id never reaches the screen")

                if mock:
                    page.locator("#diagnostics summary").click()
                    page.locator("#fixture").click()
                    expect(page.locator("#diag-status")).to_contain_text("Diagnostic turn complete")
                    raw = page.locator(
                        "#raw"
                    ).text_content()  # innerText is empty in closed <details>
                    assert "tool.allowed" in raw and "turn.completed" in raw
                    checks.append("developer panel still runs the fixture")
                raw = page.locator("#raw").text_content()
                assert keys["a"] not in raw and "session_token" not in raw

                page.locator("#text").focus()
                assert (
                    page.locator("#text").evaluate("e => getComputedStyle(e).outlineStyle")
                    != "none"
                )
                assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
                checks.append("keyboard focus outline and reduced motion")

                # Conversation mode. The fake microphone plays its file once, then silence,
                # through the real AudioWorklet. Start once and never press Stop: the silence
                # gate ends the recording, the reply plays, and the microphone opens again.
                page.evaluate(RECORD_STAGES)
                before = entries().count()
                page.locator("#talk").click()
                expect(page.locator("#talk")).to_have_text("Stop")
                expect(page.locator("#mic-level")).to_be_visible()
                expect(page.locator("#end")).to_be_enabled()
                page.wait_for_timeout(1000)
                page.locator(".conversation").screenshot(
                    path=str(OUTPUT / "listening-1512x982.png")
                )
                expect(entries()).to_have_count(before + 2, timeout=auto_stop_ms + turn)
                expect(entries().nth(before)).to_contain_text("YOU · VOICE")
                expect(page.locator("#how-steps")).to_contain_text("Speech recognition")
                expect(page.locator("#arch-node-mic.done")).to_have_count(1)
                expect(page.locator("#arch-node-asr.done")).to_have_count(1)
                if mock:
                    expect(entries().nth(before)).to_contain_text("Fixture transcript")
                checks.append("fake microphone recording stops on silence and submits")
                speaking = "Playing the test tone." if mock else "Speaking."
                expect(page.locator("#talk")).to_have_text("Stop", timeout=turn)
                expect(page.locator("#mic-level")).to_be_visible()
                stages = page.evaluate("window.recordedStages")
                times = page.evaluate("window.recordedTimes")
                listening = "Listening."
                in_order(
                    stages,
                    [listening, "Sent. Transcribing.", "Heard you. Thinking.", speaking, listening],
                )
                assert "Nothing heard. Try again." not in stages, stages
                levels = page.evaluate("window.recordedLevels")
                assert levels and max(levels) > 0, levels
                opened = times[stages.index(listening)]
                submitted = times[stages.index("Starting the conversation.")]
                measured["fake_mic_peak_meter_percent"] = max(levels)
                measured["start_to_automatic_stop_ms"] = round(submitted - opened)
                voice = page.evaluate("window.__timings")
                assert voice["input_source"] == "audio" and voice["capture"]["auto"], voice
                # The quiet the gate waited, measured in recorded audio: the 0.8 s hangover.
                assert 800 <= voice["capture"]["quietMs"] < 1000, voice["capture"]
                measured["voice_recorded_audio_ms"] = round(voice["capture"]["audioMs"])
                measured["voice_trailing_quiet_ms"] = round(voice["capture"]["quietMs"])
                if voice["marks"].get("playing") is not None:
                    measured["voice_capture_end_to_playback_ms"] = round(
                        voice["durations"]["capture_end_to_playing_ms"]
                    )
                checks.append("meter and state line follow the turn; microphone reopens after it")

                # End conversation closes the microphone, frees the session, keeps the store.
                page.locator("#end").click()
                expect(page.locator("#mic-level")).to_be_hidden()
                expect(page.locator("#talk")).to_have_text("Start conversation")
                expect(page.locator("#state-line")).to_have_text(
                    "Conversation ended. Start again or type."
                )
                expect(entries()).to_have_count(0)
                expect(page.locator("#products-label")).to_contain_text("STOREFRONT")
                expect(page.locator("#mute-mic")).to_be_disabled()
                expect(page.locator("#cart")).to_be_hidden()
                checks.append("end conversation closes the microphone and keeps the storefront")

                # Sign out clears everything; Retailer B then works on its own catalog.
                page.locator("#sign-out").click()
                expect(page.locator("#signin")).to_be_visible()
                expect(page.locator("#store")).to_be_hidden()
                assert page.locator("#raw").text_content() == "[]"
                assert page.locator("#cards li").count() == 0
                assert page.evaluate("localStorage.length + sessionStorage.length") == 0
                checks.append(
                    "sign-out clears keys, cards, transcript and events; no browser storage"
                )
                sign_in("b")
                expect(page.locator("#cards .card")).to_have_count(len(b_names))
                # A greeting as the first message leaves the storefront on screen whenever the
                # model answered it without a catalog lookup (always, in mock).
                send("Hi there")
                greeted_without_tools = "Catalog tool" not in (
                    page.locator("#how-steps").text_content() or ""
                )
                if mock:
                    expect(entries().last).to_contain_text("welcome to Harbor Supply Wharf")
                    assert greeted_without_tools
                if greeted_without_tools:
                    expect(page.locator("#products-label")).to_contain_text("STOREFRONT")
                    expect(page.locator("#cards .card")).to_have_count(len(b_names))
                send("I need a rain jacket under 150 dollars")
                assert set(card_names()) <= set(b_names) and card_names()
                if mock:
                    send("What time is it?")
                    expect(entries().last).to_contain_text("at the store (America/New_York).")
                page.screenshot(path=str(OUTPUT / "store-retailer-b.png"), full_page=True)
                page.locator("#sign-out").click()
                expect(page.locator("#signin")).to_be_visible()
                checks.append("retailer B storefront and conversation")
            except Exception:
                # Release the session on a failed run, so the next run can sign in.
                if page.locator("#sign-out").is_visible():
                    page.locator("#sign-out").click()
                raise

            assert not errors, errors
            assert not external, external
            checks.append("no page errors or external requests")

            # A recorded walkthrough on a second mock server, when ffmpeg can make one.
            folder = make_walkthrough(Path(temp.name) / "walkthrough") if mock else None
            if mock and folder is None:
                measured["walkthrough_player"] = "skipped: no ffmpeg, outline state only"
            if folder is not None:
                check_walkthrough(browser, env | {"WALKTHROUGH_DIR": str(folder)}, checks)
            browser.close()
        print(
            json.dumps(
                {
                    "result": "pass",
                    "mode": "mock" if mock else "nvidia",
                    "checks": checks,
                    "measured": measured,
                }
            )
        )
    finally:
        temp.cleanup()
        if server:
            server.terminate()
            server.wait(timeout=10)
            logs.close()


if __name__ == "__main__":
    main()
