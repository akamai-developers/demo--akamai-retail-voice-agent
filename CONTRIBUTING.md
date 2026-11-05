# Contributing

Run `python3.12 scripts/demo.py setup`, activate `.venv`, then run `make check`. Use Node.js 22 or newer for client checks. For UI changes, run the browser checks and inspect the screenshots at 1920x1080, 1512x982, 1366x1024, and 390x844.

Keep source, tests, schemas, and synthetic fixtures in the repository. Keep credentials, personal media, raw transcripts, model weights, and deployment logs in local storage. Tests use clearly named synthetic credentials. Never replace them with working credentials.

Describe the behavior changed and how it was checked. Write short, concrete explanations. Avoid marketing phrases and em dashes. Add a regression test when changing auth, input limits, tenant scope, shopping state, or deployment behavior.

Update direct dependency pins in `pyproject.toml`, then regenerate `requirements.lock.txt` with `uv pip compile pyproject.toml --extra test --generate-hashes --output-file requirements.lock.txt`. Review the resolved changes, run pip-audit, and rerun the checks. Changes to Pipecat must also update its version in the GPU profile. GPU dependency changes require a new hardware validation run.
