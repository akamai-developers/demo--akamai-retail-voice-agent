"""Local preview and presentation UI checks. Set TRYON_DIR to inspect private prepared assets."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from browser_check import ROOT, start_server
from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(os.getenv("EVIDENCE_DIR", str(ROOT / "artifacts/tryon-browser")))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(os.getenv("TRYON_DIR", temp)).resolve()
        if "TRYON_DIR" not in os.environ:
            # A catalog image is sufficient to test delivery, never evidence of image generation.
            shutil.copyfile(
                ROOT / "src/retail_voice/data/products/img-a-downpour-packable-navy.jpg",
                directory / "fixture.jpg",
            )
            (directory / "previews.json").write_text(
                json.dumps(
                    [{"product_id": "a-downpour-packable", "color": "navy", "file": "fixture.jpg"}]
                )
            )
        env = os.environ | {
            "DEMO_MODE": "mock",
            "TRYON_MODE": "prepared",
            "TRYON_DIR": str(directory),
            "TRYON_TENANT_ID": "retailer-a",
            "TENANT_A_KEY": "browser-tenant-a-fixture",
            "TENANT_B_KEY": "browser-tenant-b-fixture",
            "OPERATOR_KEY": "browser-operator-fixture",
        }
        server, logs, origin = start_server(env)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(
                    viewport={"width": 1512, "height": 982}, reduced_motion="reduce"
                )
                errors, requests = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(origin)
                page.locator("#key").fill(env["TENANT_A_KEY"])
                page.locator("#signin-button").click()
                page.locator('[data-product="a-downpour-packable"]').click()
                expect(page.locator("#tryon-button")).to_be_visible()
                page.locator("#text").fill("I like this item. Let me see it on me in navy medium.")
                page.locator("#send").click()
                expect(page.locator("#tryon-photo img")).to_be_visible()
                page.wait_for_function(
                    "document.querySelector('#tryon-photo img')?.naturalWidth > 0"
                )
                expect(page.locator("#tryon-label")).to_have_text("Prepared AI preview · navy")
                expect(page.locator("#tryon-note")).to_contain_text("not a size or fit prediction")
                assert not any(
                    url.endswith("/audio") or url.endswith("/result") for url in requests
                ), requests
                preview = next(url for url in requests if url.endswith("/preview"))
                assert page.request.get(preview).status == 401
                for width, height in [(1920, 1080), (1512, 982), (1366, 1024), (390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.locator("#tryon").screenshot(
                        path=str(output / f"preview-{width}x{height}.png")
                    )
                page.locator("#tryon-close").click()
                expect(page.locator("#tryon")).to_be_hidden()
                page.locator("#how-toggle").click()
                page.locator("#stage-toggle").click()
                expect(page.locator("#request-stage")).to_be_visible()
                expect(page.locator("#how-panel-arch")).to_be_visible()
                expect(page.locator(".how-tabs")).to_be_hidden()
                expect(page.locator("#request-stage")).to_contain_text("Illustrative flow")
                expect(page.locator("#platform-current")).to_contain_text("local mock")
                for width, height in [(1920, 1080), (1512, 982), (1366, 1024), (390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.locator("#how").screenshot(
                        path=str(output / f"stage-{width}x{height}.png")
                    )
                page.locator("#stage-toggle").click()
                expect(page.locator("#how-panel-arch")).to_be_visible()
                page.locator("#how-toggle").click()
                page.locator("#sign-out").click()
                expect(page.locator("#tryon")).to_be_hidden()
                expect(page.locator("#tryon-photo img")).to_have_count(0)
                assert not errors, errors
                browser.close()
            print(
                "PASS: private prepared preview, inline audio/result, close/sign-out cleanup, presentation view, four viewports"
            )
        finally:
            server.terminate()
            server.wait(timeout=10)
            logs.close()


if __name__ == "__main__":
    main()
