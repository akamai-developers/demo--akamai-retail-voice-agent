"""Check the presentation diagram against the real app and its CSP, without GPU calls."""

import json
import os
from pathlib import Path

from browser_check import ROOT, start_server
from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(os.getenv("EVIDENCE_DIR", str(ROOT / "artifacts/architecture-browser")))
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "DEMO_MODE": "mock",
        "TENANT_A_KEY": "browser-tenant-a-fixture",
        "TENANT_B_KEY": "browser-tenant-b-fixture",
        "OPERATOR_KEY": "browser-operator-fixture",
    }
    server, logs, origin = start_server(env)
    report = {"errors": [], "screens": [], "checks": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1512, "height": 982})
            requests = []
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            page.on(
                "console",
                lambda m: report["errors"].append(m.text) if m.type == "error" else None,
            )
            page.on("request", lambda r: requests.append((r.method, r.url)))
            page.goto(origin)
            stage = page.locator("#request-stage")
            expect(stage).to_have_attribute("data-playing", "false")
            page.locator("#how-toggle").click()
            expect(stage).to_be_visible()
            expect(stage).to_have_attribute("data-playing", "true")
            expect(page.locator("#platform-current")).to_contain_text("local mock")
            expect(page.locator("#arch")).to_be_hidden()
            page.wait_for_timeout(350)
            point = page.locator(".flow-marker").get_attribute("cx")
            page.wait_for_timeout(350)
            assert point != page.locator(".flow-marker").get_attribute("cx")
            page.locator("#flow-play").click()
            expect(stage).to_have_attribute("data-playing", "false")
            point = page.locator(".flow-marker").get_attribute("cy")
            page.wait_for_timeout(150)
            assert point == page.locator(".flow-marker").get_attribute("cy")
            page.locator("#flow-next").click()
            expect(stage).to_have_attribute("data-step", "2")
            page.locator("#flow-previous").click()
            expect(stage).to_have_attribute("data-step", "1")
            page.locator("#flow-scenario").select_option("tryon")
            for _ in range(9):
                page.locator("#flow-next").click()
            expect(page.locator(".flow-heading")).to_have_text("Generate the try-on preview")
            page.screenshot(path=str(output / "tryon-flow.png"))
            page.locator("#flow-replay").click()
            expect(stage).to_have_attribute("data-playing", "true")
            page.locator("#how-tab-live").click()
            expect(stage).to_have_attribute("data-playing", "false")
            expect(page.locator("#arch")).to_be_visible()
            page.locator("#arch-node-gateway").click()
            expect(page.locator("#arch-detail-title")).to_have_text("Gateway and admission")
            page.locator("#how-tab-video").click()
            expect(page.locator("#how-panel-video")).to_be_visible()
            page.locator("#how-tab-arch").click()
            page.locator("#flow-scenario").select_option("voice")
            page.locator("#flow-next").click()
            page.locator("#flow-next").click()
            page.locator("#flow-next").click()
            report["checks"] += [
                "Playback, pause, replay, and manual steps",
                "Try-on branch and voice scenario",
                "Live diagnostics preserved in separate tab",
                "Animation pauses when its tab is hidden",
            ]
            for width, height in [(1920, 1080), (1512, 982), (1366, 1024), (390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(output / f"architecture-{width}x{height}.png"))
                page.locator("#stage-toggle").click()
                expect(page.locator(".how-tabs")).to_be_hidden()
                expect(stage).to_be_visible()
                bounds = stage.bounding_box()
                assert bounds["y"] + bounds["height"] <= height + 1
                page.screenshot(path=str(output / f"presentation-{width}x{height}.png"))
                page.locator("#stage-toggle").focus()
                page.keyboard.press("Escape")
                expect(page.locator(".how-tabs")).to_be_visible()
                report["screens"].append([width, height])
            page.locator("#flow-play").click()
            page.locator("#how-toggle").click()
            expect(stage).to_have_attribute("data-playing", "false")
            expect(page.locator("#signin")).to_be_visible()
            # The explanatory flow must never start a real session or inference request.
            assert not any(method == "POST" for method, _ in requests), requests
            assert all(url.startswith(origin + "/") for _, url in requests), requests
            report["checks"].append("No session, inference, or external requests from animation")
            reduced = browser.new_page(reduced_motion="reduce")
            reduced.goto(origin + "/#how")
            expect(reduced.locator("#request-stage")).to_be_visible()
            expect(reduced.locator("#flow-play")).to_be_disabled()
            expect(reduced.locator(".flow-marker")).to_have_attribute("visibility", "hidden")
            reduced.locator("#flow-next").click()
            expect(reduced.locator("#request-stage")).to_have_attribute("data-step", "2")
            report["checks"].append("Reduced motion uses static manual steps")
            assert not report["errors"], report["errors"]
            browser.close()
        (output / "checks.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        server.terminate()
        server.wait(timeout=10)
        logs.close()


if __name__ == "__main__":
    main()
