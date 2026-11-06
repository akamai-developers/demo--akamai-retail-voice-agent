# Validation

The application passes local and isolated GPU functional checks. It is not cleared for public release because GPU dependency advisories remain unresolved. The source is Apache-2.0; model and asset licenses are separate. Existing deployments were not modified.

## Reproduce local checks

The November 5, 2026 run passed 266 Python tests, formatting, lint, type checks, JavaScript client/audio/transport checks, and fresh wheel installation. The browser suites passed at all four documented sizes, and all 48 synthetic catalog images matched their hashes. The exported archive installed independently, denied unauthenticated catalog access, and kept retailer catalogs separate. These checks are not a macOS execution receipt.

```bash
python3.12 scripts/demo.py setup
. .venv/bin/activate
make check
python -m playwright install chromium
make test-ui test-tryon-ui test-architecture-ui
make test-package
```

The checks cover authentication, tenant scope, bounded input, conversation state, stock, cart updates, prepared previews, browser audio, and wheel installation. Prepared previews use synthetic images and do not establish real inference quality.

`scripts/public_check.py` rejects unexpected release files, em dashes, development markers, and selected credential patterns. Gitleaks performs a separate secret scan. `scripts/release.py` exports allowlisted source files and excludes private configuration, keys, models, media, test artifacts, and git history. Its `--publish` flag requires a source license; it does not certify security readiness.

## Dependency findings

Install `pip-audit==2.10.1` in a separate tooling environment, then run:

```bash
pip-audit -r requirements.lock.txt
python scripts/audit_gpu.py
```

The gateway lock and the 83 packages installed in the new GPU gateway passed their advisory scans. Full installed GPU environment scans found:

| Environment | Packages scanned | Affected packages |
| --- | ---: | ---: |
| Speech | 187 | 4 |
| Image worker | 108 | 4 |
| Model-serving container | 241 | 27 |

Speech findings include Torch 2.8.0, fsspec 2025.12.0, hydra-core 1.3.2, and Lightning 2.4.0. Image-worker findings include Torch 2.8.0, Diffusers 0.37.1, Transformers 4.57.3, and Accelerate 1.12.0. The installed package and advisory IDs appear in [the hardware receipt](ams-verification.json). Some advisories have no listed fixed version. Audit failures remain visible; none were suppressed.

Both GPU dependency sets resolved on Linux with Python 3.12. An upgrade probe found no advisories in newer direct releases, but they did not form a supported NeMo installation. NeMo Speech revision `f6f61caaab4b13c94c787e7c4b75ab24418a79ed` requires Lightning greater than 2.2.1 and no newer than 2.4.0. Resolving it with Lightning 2.6.6 fails. The installer does not bypass that constraint. [The earlier dependency receipt](dependency-audit.json) records this resolution failure and the direct-pin scans.

Release requires a compatible dependency set that passes the full installed-environment audit and repeated CUDA/model checks. PyPI advisory scans do not review the pinned NeMo source, container operating-system packages, or Caddy. Torch CUDA build suffixes were normalized for advisory lookup; the source-installed NeMo package was excluded from that package-index scan. No Caddy instance was deployed during this hardware verification.

## Hardware checks

[The Amsterdam verification](verification.md) records actual speech inference, live preview generation, cart continuity, stock handling, retailer isolation, browser behavior, firewall probes, and latency. It used a dedicated 96 GB GPU and the pinned stack at commit `8aa9ae6a3a628bd81fddf629fcfd4efbab8e47ea`. It does not validate a future dependency upgrade, smaller GPU, San Francisco client, or public HTTPS deployment. Human listening and concurrent peak-memory checks remain pending.

Repeat on your own dedicated host:

1. Follow [the deployment guide](deploy.md). Retain the source commit, resolved packages, and model revisions privately.
2. Run preflight. Confirm API listeners use loopback and firewall rules block private ports externally. Verify HTTPS and allowed/disallowed sources if deploying a proxy.
3. Warm the models, then run [the latency workload](latency.md) from the intended presentation location.
4. Select an in-stock product, color, and size, request a preview, then add it to the cart. Confirm the same variant is added once.
5. Request a sold-out variant. Confirm it is neither previewed nor added. Switch products by voice and verify the selection changes.
6. Repeat with the second retailer. Confirm catalog and session isolation, listen to full replies, and inspect previews. Keep personal photos and recordings private.
7. Restart services and repeat a turn. Separately test the documented quarantine recovery after controlled interrupted inference. Measure peak GPU memory under your intended load.

Use `retail_voice.cli smoke-gpu --allow-inference` with permitted local fixtures for automated checks; see `python -m retail_voice.cli --help`. Passing a smoke report alone does not certify natural speech, image quality, firewall behavior, or dependency security.
