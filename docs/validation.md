# Validation

This review copy is not cleared for public release. The GPU dependency audit reports unresolved advisories. The repository owner selected Apache-2.0 for the application source. The existing deployment was not changed during release preparation.

## Local checks

The 2026-11-05 Linux check passed 262 Python tests, formatting, lint, type checks, and the JavaScript client/audio/transport checks. All three browser suites passed; storefront screenshots were inspected at all four documented sizes. A fresh wheel installation passed its asset smoke check, and all 48 synthetic catalog images matched their recorded hashes. The exported archive installed and started independently, denied unauthenticated catalog access, and kept the two retailer catalogs separate. Gitleaks 8.30.1 found no secrets in the source-only review archive. These results do not constitute a GPU or macOS execution receipt.

Run these checks from a fresh clone:

```bash
python3.12 scripts/demo.py setup
. .venv/bin/activate
make check
python -m playwright install chromium
make test-ui test-tryon-ui test-architecture-ui
make test-package
```

The checks cover authentication, tenant scope, bounded input, conversation state, stock, cart updates, prepared previews, browser audio handling, and installation from a wheel. Prepared previews in tests use synthetic images. They do not establish real inference quality.

`scripts/public_check.py` rejects unexpected release files, em dashes, development markers, and selected credential patterns. Gitleaks performs a separate secret scan. `scripts/release.py` exports only allowlisted source files; it excludes local configuration, keys, models, media, test artifacts, and git history. Use `--publish` to require a source license.

## Dependency checks

Install `pip-audit==2.10.1` in a separate tooling environment, then run:

```bash
pip-audit -r requirements.lock.txt
python scripts/audit_gpu.py
```

On 2026-11-05, the updated gateway lock passed the advisory scan. The existing GPU direct pins did not: Torch 2.8.0, Diffusers 0.37.1, Transformers 4.57.3, and Accelerate 1.12.0 have advisories. Some have no listed fixed version. The audit command and CI retain these failures rather than suppressing them.

Both GPU dependency sets resolved on Linux with Python 3.12. Auditing the full speech resolution also found advisories in fsspec 2025.12.0, hydra-core 1.3.2, and Lightning 2.4.0. The image-worker resolution reported the same four affected packages as its direct-pin scan. [The audit receipt](dependency-audit.json) lists versions, advisory IDs, fixed-version information, and scan coverage without raw deployment logs.

An upgrade probe found no advisories in the latest direct package releases, but they do not form a supported NeMo installation. NeMo Speech revision `f6f61caaab4b13c94c787e7c4b75ab24418a79ed` requires Lightning greater than 2.2.1 and no newer than 2.4.0. Resolving it with Lightning 2.6.6 fails. The old pins remain visible here; the installer does not bypass the dependency constraint. Resolving this requires a supported NeMo update or a separately tested source patch, followed by CUDA and model validation.

Resolve those findings with a compatible, audited GPU dependency set, then install it on hardware. Do not infer NeMo compatibility from a successful gateway test. Audit the actual installed speech/image environments again on the host. The pinned NeMo source, vLLM container, and Caddy container also need review. The direct-pin CI scan does not cover their complete dependency trees or operating-system packages.

## Hardware checks

Status: **[Unverified: Hardware Run Pending]** for this public release's updated dependencies and generated deployment path.

1. Install on the target GPU host using [the deployment guide](deploy.md). Save resolved dependencies and model revisions privately.
2. Confirm preflight passes and all model ports bind to loopback. Probe them from a separate host and confirm they are unreachable. Verify HTTPS from an allowed source and rejection from a disallowed source.
3. Ask for a raincoat under $150. Select an exact product, color, and size. Verify the stock response against the synthetic catalog.
4. Request a preview of an in-stock variant, then say `Add it to my cart`. Confirm the same product, color, and size is added once.
5. Request an out-of-stock variant. Confirm the app offers alternatives and does not generate a misleading preview or add it.
6. Switch products by voice. Confirm the selection changes. Repeat with the second retailer and confirm catalog and session isolation.
7. Listen to complete replies and inspect the preview. Record latency from a real client and peak GPU memory during speech and image work. Keep raw recordings and reference photos private.
8. Restart services and repeat a turn. Exercise the documented quarantine recovery after a controlled interrupted inference.

For automated hardware checks, use `retail_voice.cli smoke-gpu --allow-inference` with permitted local audio and product-image fixtures. See `python -m retail_voice.cli --help`. A smoke report cannot certify natural speech, image quality, or firewall behavior by itself.
