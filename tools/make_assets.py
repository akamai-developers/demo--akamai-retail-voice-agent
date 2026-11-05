"""Make the product photos and write the asset manifest.

Reads src/retail_voice/data/catalog.json. Keeps one JPEG per product color in
src/retail_voice/data/products/ and the manifest src/retail_voice/data/assets.json.

The photos are generated with OpenAI gpt-image-1.5 from the prompt that prompt_for() builds
for each product and color, then resized to 512 x 512 and encoded as a baseline JPEG here.
The products are fictional; no third-party photograph, logo or mark is involved. Generation
is not reproducible, so the generated files are committed and `--check` verifies what is on
disk instead: every catalog image id has a 512 x 512 JPEG, nothing extra is in the folder,
and the manifest regenerates byte for byte from those files and the catalog.

Usage, from reference-app/ with the project venv active:
    OPENAI_API_KEY=... python tools/make_assets.py --generate            # all 48 photos
    OPENAI_API_KEY=... python tools/make_assets.py --generate --only img-jacket-a-navy
    python tools/make_assets.py --import-dir ~/photos   # photos made elsewhere, by image id
    python tools/make_assets.py            # rewrite assets.json from the files on disk
    python tools/make_assets.py --check    # verify the committed files and manifest
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "retail_voice" / "data"
PRODUCTS = DATA / "products"
CREATED = "2026-11-02"
MODEL = "gpt-image-1.5"
BASIS = "AI-generated image of a fictional product, no third-party photo, logo or mark"
REVIEW = "pending review"
SIZE = 512
QUALITY = 88
JPEG_SIGNATURE = b"\xff\xd8\xff"

# What each product looks like, in the words a studio photographer would get.
LOOKS = {
    "jacket-a": "lightweight waterproof rain shell jacket with an adjustable hood, a full front "
    "zip and a matte ripstop nylon finish",
    "a-trailhead-softshell": "stretch softshell jacket with a stand-up collar and no hood, a full "
    "front zip and a smooth woven fabric",
    "a-summit-down-jacket": "hooded 700-fill down puffer jacket with horizontal baffles and a "
    "full front zip",
    "a-city-commuter-jacket": "water-resistant city commuter jacket with a hood, thin reflective "
    "trim stripes and a clean urban cut",
    "a-downpour-packable": "ultralight packable waterproof rain jacket with a hood and a thin "
    "crinkled ripstop fabric",
    "a-stormline-pro-shell": "three-layer waterproof breathable alpine shell jacket with a large "
    "helmet-compatible hood and taped seams",
    "a-drizzle-anorak": "pullover rain anorak with a half zip, a large front kangaroo pocket and "
    "a hood",
    "a-basecamp-fleece": "midweight full-zip fleece jacket with a stand-up collar",
    "a-sherpa-pullover": "high-pile sherpa fleece pullover with a half zip and a front pocket",
    "a-grid-fleece-hoodie": "lightweight grid fleece hoodie with a full zip and a visible "
    "grid-pattern fleece texture",
    "a-ridge-puffer-vest": "insulated puffer vest with horizontal baffles, no sleeves and a high "
    "collar",
    "a-trail-fleece-vest": "sleeveless fleece vest with a full zip and a stand-up collar",
    "a-rain-bucket-hat": "waterproof bucket hat with a wide brim",
    "a-merino-beanie": "merino wool knit beanie with a folded cuff and a small pom on top",
    "a-trail-sun-cap": "lightweight hiking cap with a curved visor and a vented mesh crown",
    "a-stormproof-gloves": "waterproof insulated winter gloves with a wrist cinch",
    "a-touchscreen-liners": "thin stretch liner gloves with touchscreen fingertips",
    "a-expedition-mitts": "down-filled expedition mittens with a long gauntlet cuff",
    "jacket-b": "water-resistant sailing deck jacket with a corduroy collar, no hood and a full "
    "front zip",
    "b-squall-rain-shell": "waterproof sailing rain jacket with a high collar, a hood and a heavy "
    "coated fabric",
    "b-dockside-fleece": "quarter-zip fleece pullover with a collar and a front chest pocket",
    "b-mooring-vest": "insulated sailing vest with deep hand pockets, no sleeves and a collar",
    "b-harbor-watch-cap": "ribbed knit watch cap",
    "b-sailing-gloves": "synthetic leather sailing gloves with open fingertips and grip palms",
}

STUDIO = (
    "Professional e-commerce catalog product photograph of a solid {color} {look}, {pose}, "
    "centered on a plain white studio background, soft even studio lighting, realistic "
    "fabric texture, sharp focus. No people, no text, no logos, no props, no hanger."
)
POSES = {
    "hats": "shown alone from a slight three-quarter front angle",
    "gloves": "shown as a pair laid flat side by side with the palms facing down",
    "upper": "displayed on an invisible ghost mannequin, front view",
}


def prompt_for(product, color):
    pose = POSES.get(product["category"], POSES["upper"])
    if "grip" in product["tags"]:
        pose = pose.replace("facing down", "facing up")
    return STUDIO.format(color=color, look=LOOKS[product["product_id"]], pose=pose)


def describe(product, color):
    tags = set(product["tags"])
    detail = (
        "hood"
        if "hooded" in tags
        else "collar"
        if "collar" in tags
        else "brim"
        if "brim" in tags
        else "visor"
        if "visor" in tags
        else None
    )
    text = f"Product photo of the {product['name']} in {color}"
    return text + (f", with a {detail}." if detail else ".")


def generate(prompt):
    """One 1024 x 1024 image from the OpenAI Images API, as PNG bytes."""
    body = json.dumps(
        {"model": MODEL, "prompt": prompt, "size": "1024x1024", "quality": "medium", "n": 1}
    ).encode()
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return base64.b64decode(json.load(response)["data"][0]["b64_json"])


def encode(source):
    """Pad to a square on white, resize to the catalog size and write a plain baseline JPEG."""
    image = Image.open(io.BytesIO(source)).convert("RGB")
    if image.width != image.height:
        side = max(image.size)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        image = square
    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()


def source_commit(explicit):
    if explicit:
        return explicit
    path = Path(__file__).resolve()
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("No git history here. Pass --source-commit with the tool's commit.")
    if dirty or not commit:
        raise SystemExit("Commit tools/make_assets.py first, or pass --source-commit.")
    return commit


def wanted():
    """Every image the catalog references: image id -> (product, color, variant ids)."""
    catalog = json.loads((DATA / "catalog.json").read_text())
    images = {}
    for product in catalog["products"]:
        colors = {}
        for variant in product["variants"]:
            colors.setdefault(variant["image_id"], (variant["color"], []))[1].append(
                variant["variant_id"]
            )
        for image_id in product["image_ids"]:
            color, variant_ids = colors[image_id]
            images[image_id] = (product, color, variant_ids)
    return catalog["catalog_version"], images


def check_file(path):
    """A committed photo is a 512 x 512 baseline JPEG, or the manifest refuses to describe it."""
    data = path.read_bytes()
    if not data.startswith(JPEG_SIGNATURE):
        raise SystemExit(f"{path.name} is not a JPEG")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "JPEG" or image.size != (SIZE, SIZE) or image.mode != "RGB":
            raise SystemExit(f"{path.name} is not a {SIZE} x {SIZE} RGB JPEG")
    return data


def build(commit):
    version, images = wanted()
    entries = []
    for image_id, (product, color, variant_ids) in images.items():
        filename = f"{image_id}.jpg"
        path = PRODUCTS / filename
        if not path.is_file():
            raise SystemExit(f"{filename} is missing. Run --generate --only {image_id}.")
        data = check_file(path)
        entries.append(
            {
                "image_id": image_id,
                "tenant_id": product["tenant_id"],
                "product_id": product["product_id"],
                "variant_ids": variant_ids,
                "color": color,
                "filename": filename,
                "mime_type": "image/jpeg",
                "alt_text": describe(product, color),
                "sha256": hashlib.sha256(data).hexdigest(),
                "provenance": "synthetic",
                "generator": f"OpenAI {MODEL}, prompt and JPEG encode by tools/make_assets.py "
                f"at commit {commit}",
                "created": CREATED,
                "redistribution_basis": BASIS,
                "review_status": REVIEW,
            }
        )
    rows = ",\n".join("    " + json.dumps(e, ensure_ascii=False) for e in entries)
    text = (
        "{\n"
        '  "schema_version": 1,\n'
        f'  "catalog_version": {json.dumps(version)},\n'
        f'  "assets": [\n{rows}\n  ]\n'
        "}\n"
    )
    return {e["filename"] for e in entries}, text


def generate_all(only):
    _, images = wanted()
    todo = {i: v for i, v in images.items() if not only or i in only}
    missing = set(only or []) - set(images)
    if missing:
        raise SystemExit(f"Unknown image ids: {sorted(missing)}")
    PRODUCTS.mkdir(parents=True, exist_ok=True)

    def one(item):
        image_id, (product, color, _) = item
        data = encode(generate(prompt_for(product, color)))
        (PRODUCTS / f"{image_id}.jpg").write_bytes(data)
        print(json.dumps({"generated": image_id, "bytes": len(data)}), flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, todo.items()))


def import_all(folder):
    """Photos made elsewhere, named <image id>.<ext> or <product id>-<color>.<ext>."""
    _, images = wanted()
    names = {i: i for i in images}
    names |= {f"{p['product_id']}-{c}": i for i, (p, c, _) in images.items()}
    PRODUCTS.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(Path(folder).iterdir()):
        image_id = names.get(path.stem.lower())
        if image_id is None or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            print(json.dumps({"skipped": path.name}), flush=True)
            continue
        (PRODUCTS / f"{image_id}.jpg").write_bytes(encode(path.read_bytes()))
        count += 1
    print(json.dumps({"imported": count}))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify files on disk, write nothing")
    parser.add_argument("--generate", action="store_true", help="call the image API first")
    parser.add_argument("--only", nargs="*", help="image ids to generate, default all")
    parser.add_argument("--import-dir", help="encode photos from this folder instead")
    parser.add_argument("--source-commit", help="commit of this tool when git is unavailable")
    args = parser.parse_args()
    if args.import_dir:
        import_all(args.import_dir)
    if args.generate:
        generate_all(args.only)
    commit = args.source_commit
    if not commit and args.check:
        existing = json.loads((DATA / "assets.json").read_text())["assets"]
        commit = existing[0]["generator"].rsplit(" ", 1)[-1]
    files, manifest = build(source_commit(commit))
    extra = sorted(p.name for p in PRODUCTS.iterdir() if p.name not in files)
    if args.check:
        problems = []
        if extra:
            problems.append(f"unexpected files: {extra}")
        if (DATA / "assets.json").read_text() != manifest:
            problems.append("assets.json differs from the files on disk")
        if problems:
            print(json.dumps({"status": "fail", "problems": problems}))
            return 1
        print(json.dumps({"status": "pass", "images": len(files)}))
        return 0
    if extra:
        raise SystemExit(f"Remove these from {PRODUCTS.name}/ first: {extra}")
    (DATA / "assets.json").write_text(manifest)
    print(json.dumps({"status": "written", "images": len(files)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
