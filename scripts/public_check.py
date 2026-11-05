"""Check the files eligible for a public release. Findings never include matched values."""

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("src", "tests", "contracts", "tools", "scripts", "deploy", "docs", ".github")
FILES = (
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE",
    "Makefile",
    "pyproject.toml",
    "requirements.lock.txt",
    "MANIFEST.in",
    ".gitignore",
    ".editorconfig",
    ".gitattributes",
    ".env.example",
    ".gitleaks.toml",
)
SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
    ".html",
    ".css",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".svg",
    ".woff2",
    ".ttf",
    ".jpg",
}
SKIP = {"__pycache__", ".mypy_cache", ".ruff_cache"}


def release_files(root=ROOT):
    for name in FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ValueError(f"Release symlink: {name}")
            yield path
    for name in DIRECTORIES:
        folder = root / name
        if folder.is_symlink():
            raise ValueError(f"Release symlink: {name}")
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if any(part in SKIP or part.endswith(".egg-info") for part in path.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Release symlink: {path.relative_to(root)}")
            if not path.is_file() or path.suffix == ".pyc":
                continue
            if path.suffix not in SUFFIXES and path.name not in {"Dockerfile", "Caddyfile"}:
                raise ValueError(f"Unexpected release file: {path.relative_to(root)}")
            if path.suffix in {".jpg", ".woff2", ".ttf", ".svg"}:
                permitted = {
                    ".jpg": root / "src/retail_voice/data/products",
                    ".woff2": root / "src/retail_voice/web/fonts",
                    ".ttf": root / "src/retail_voice/web/fonts",
                    ".svg": root / "src/retail_voice/web/brand",
                }
                if path.parent != permitted[path.suffix]:
                    raise ValueError(f"Unexpected binary or image asset: {path.relative_to(root)}")
            yield path


def check(root=ROOT, publish=False):
    failures = []
    paths = list(release_files(root))
    # In a standalone repository, CI must also reject tracked files outside the export set.
    # Do not inspect a parent workspace's unrelated history when preparing a review copy.
    git_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True
    )
    if git_root.returncode == 0 and Path(git_root.stdout.strip()).resolve() == root.resolve():
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
        allowed = {path.relative_to(root).as_posix() for path in paths} | {"release-manifest.json"}
        failures.extend(
            f"{name}: tracked file is outside the public allowlist"
            for name in tracked
            if name and name not in allowed
        )
    rules = {
        "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "credential URL": re.compile(r"https?://[^\s/]+:[^\s/]+@"),
        "embedded token": re.compile(
            r"(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{30,})"
        ),
        "em dash": re.compile(chr(0x2014)),
        "development marker": re.compile(r"\b(?:TO" + r"DO|FIX" + r"ME|HA" + r"CK|T" + r"BD)\b"),
    }
    for path in paths:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        # Vendor notices are retained verbatim. Never rewrite third-party licenses.
        if path.parent == root / "src/retail_voice/web/fonts":
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in rules.items():
                if pattern.search(line):
                    failures.append(f"{path.relative_to(root)}:{number}: {label}")
    if publish and not (root / "LICENSE").is_file():
        failures.append(
            "LICENSE: repository owner must select the source license before publication"
        )
    if failures:
        raise ValueError("\n".join(failures))
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish", action="store_true", help="also require an application license"
    )
    args = parser.parse_args()
    print(f"Public file checks passed: {len(check(publish=args.publish))} files")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
