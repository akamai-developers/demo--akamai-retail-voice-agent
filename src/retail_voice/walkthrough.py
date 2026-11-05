"""The optional recorded walkthrough.

The gateway serves a walkthrough only when WALKTHROUGH_DIR names a directory holding
walkthrough.json, walkthrough.mp4 and walkthrough.vtt. They are checked once, when the app is
created; a set but broken directory stops startup instead of showing a broken tab. The files are
served without a key, like the other page assets: a walkthrough is reviewed footage of the demo
with no credentials or shopper data, and a video element cannot send a bearer token anyway.
"""

import json
import re
from pathlib import Path

# The only names the route serves, with their content types.
FILES = {
    "walkthrough.json": "application/json",
    "walkthrough.mp4": "video/mp4",
    "walkthrough.vtt": "text/vtt; charset=utf-8",
}
BUILD = re.compile(r"[A-Za-z0-9._+-]{4,64}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
REQUIRED = {"build", "recorded", "captions", "chapters"}
ALLOWED = REQUIRED | {"schema_version", "title", "duration_seconds"}
MAX_CHAPTERS = 20


class WalkthroughError(RuntimeError):
    """WALKTHROUGH_DIR is set but cannot be served as described."""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def validate(manifest):
    """Check walkthrough.json against contracts/walkthrough.schema.json, by hand: the schema
    library is a test dependency only."""
    if not isinstance(manifest, dict) or set(manifest) - ALLOWED or REQUIRED - set(manifest):
        raise WalkthroughError("walkthrough.json needs build, recorded, captions and chapters only")
    if manifest.get("schema_version", 1) != 1:
        raise WalkthroughError("walkthrough.json schema_version must be 1")
    if not isinstance(manifest["build"], str) or not BUILD.fullmatch(manifest["build"]):
        raise WalkthroughError("walkthrough.json build must be a commit or version label")
    if not isinstance(manifest["recorded"], str) or not DATE.fullmatch(manifest["recorded"]):
        raise WalkthroughError("walkthrough.json recorded must be a YYYY-MM-DD date")
    if manifest["captions"] != "walkthrough.vtt":
        raise WalkthroughError("walkthrough.json captions must be walkthrough.vtt")
    title = manifest.get("title", "")
    if not isinstance(title, str) or len(title) > 120:
        raise WalkthroughError("walkthrough.json title must be text up to 120 characters")
    if "duration_seconds" in manifest and not _number(manifest["duration_seconds"]):
        raise WalkthroughError("walkthrough.json duration_seconds must be a number of seconds")
    chapters = manifest["chapters"]
    if not isinstance(chapters, list) or not 1 <= len(chapters) <= MAX_CHAPTERS:
        raise WalkthroughError("walkthrough.json needs 1 to 20 chapters")
    previous = -1.0
    for chapter in chapters:
        if (
            not isinstance(chapter, dict)
            or set(chapter) != {"start", "title"}
            or not _number(chapter["start"])
            or not isinstance(chapter["title"], str)
            or not 1 <= len(chapter["title"]) <= 120
        ):
            raise WalkthroughError("each chapter needs a start in seconds and a title")
        if chapter["start"] <= previous:
            raise WalkthroughError("chapter starts must increase")
        previous = chapter["start"]
    return manifest


def load(directory):
    """The walkthrough's files and manifest, or None when WALKTHROUGH_DIR is unset."""
    if not directory:
        return None
    given = Path(directory)
    if not given.is_absolute():
        raise WalkthroughError("WALKTHROUGH_DIR must be an absolute path")
    if not given.is_dir():
        raise WalkthroughError("WALKTHROUGH_DIR is not a directory")
    root = given.resolve()
    files = {}
    for name in FILES:
        path = (root / name).resolve()
        # A link inside the directory must not lead outside it.
        if path.parent != root or not path.is_file():
            raise WalkthroughError(f"WALKTHROUGH_DIR needs a regular {name}")
        files[name] = path
    try:
        manifest = json.loads(files["walkthrough.json"].read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise WalkthroughError("walkthrough.json is not valid JSON") from None
    validate(manifest)
    with files["walkthrough.vtt"].open("rb") as captions:
        head = captions.read(9)
    if not head.removeprefix(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
        raise WalkthroughError("walkthrough.vtt must be a WebVTT file")
    if files["walkthrough.mp4"].stat().st_size < 1024:
        raise WalkthroughError("walkthrough.mp4 is too small to be a recording")
    return {"manifest": manifest, "files": files}
