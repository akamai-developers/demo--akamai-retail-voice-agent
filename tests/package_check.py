"""Run against the installed wheel from outside the source tree."""

import os

from fastapi.testclient import TestClient

from retail_voice.api import create_app
from retail_voice.settings import model_manifest

assert model_manifest()["models"]["vlm"]["revision"]

os.environ["DEMO_MODE"] = "mock"
with TestClient(
    create_app(
        {"package-fixture-a-key": "retailer-a", "package-fixture-b-key": "retailer-b"},
        "package-fixture-operator",
    )
) as client:
    for path in [
        "/",
        "/app/app.js",
        "/app/transport.js",
        "/app/capture.js",
        "/app/conversation.js",
        "/app/diagnostics.js",
        "/app/architecture.js",
        "/app/walkthrough.js",
        "/app/app.css",
        "/app/audio.mjs",
        "/app/vad.js",
        "/app/recorder-worklet.js",
        "/app/brand/akamai-logo.svg",
        "/app/brand/akamai-logo-white.svg",
        "/app/fonts/InstrumentSans.ttf",
        "/app/fonts/JetBrainsMono.ttf",
        "/app/fonts/OFL-InstrumentSans.txt",
        "/app/fonts/OFL-JetBrainsMono.txt",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content, path
    # The packaged catalog and product images load from the installed wheel.
    seen = {}
    for key in ("package-fixture-a-key", "package-fixture-b-key"):
        headers = {"Authorization": "Bearer " + key}
        catalog = client.get("/v1/catalog", headers=headers)
        assert catalog.status_code == 200, key
        products = catalog.json()["products"]
        assert products, key
        seen[key] = {p["product_id"] for p in products}
        image = client.get(products[0]["image_path"], headers=headers)
        assert image.status_code == 200 and image.content.startswith(b"\xff\xd8\xff"), key
    assert not seen["package-fixture-a-key"] & seen["package-fixture-b-key"]
print(
    "PASS: installed wheel includes model profile, catalog, product images, microphone assets,"
    " HTML, JS, CSS, logo, fonts and licenses"
)
