# Validation

The underlying installer and runtime passed end-to-end verification on a newly provisioned Amsterdam GPU. It is cleared for a restricted developer demo with explicit acceptance of the documented unfixed Ubuntu advisories. It has not been pushed. [Fresh-site results and latency](fresh-site-verification.md) and [the receipt](fresh-site-verification.json) are the current evidence.

The run covers fresh provisioning, model downloads, runtime installation, 298 Python tests, static and JavaScript checks, independent source-export installation, real speech, live preview-to-cart behavior, browser layouts, dependency and image scans, deployment resume, and automatic recovery after reboot. It uses only the bundled fictional person for try-on. Existing deployments and firewalls were unchanged.

## Cloud command interface

The Make commands added after that hardware run wrap the same installer. Local tests cover settings, administrator address validation, cancellation, saved-state resume, key permissions, loopback tunneling, scoped deletion, and interrupted cleanup. No cloud resources were created or modified to test this interface. A complete hardware run through the new Make entry points remains unverified.

## Reproduce

```bash
python3.12 scripts/demo.py setup
. .venv/bin/activate
make check
python -m playwright install chromium
make test-ui test-tryon-ui test-architecture-ui
make test-package
```

For hardware verification, follow [Deploy one site](deploy.md), then run the [latency workload](latency.md). Warm the models after service restart. Select an in-stock variant, generate a preview, and add that same variant once. Confirm a sold-out variant cannot be previewed or added. Repeat with the second retailer, check private ports, and test recovery after reboot. Retain raw logs privately.

`scripts/public_check.py` checks eligible release files, image provenance, selected credential patterns, and writing constraints. Gitleaks separately scans the exported source and Git history. `scripts/release.py` exports allowlisted files without credentials, personal photos, local runtime state, or Git history. `--publish` checks local release readiness; it does not push anything.

## Limits

Host findings remain visible. Available fixes, incomplete scans, and application dependency or container findings still block deployment. `--accept-host-advisories` accepts only unfixed host findings for a restricted demonstration. Read [Security](../SECURITY.md) before deploying. Passing scans cannot establish the absence of vulnerabilities.

The actual browser test used SSH to localhost. Public certificate issuance, Ubuntu 26.04, smaller GPUs, macOS execution, San Francisco latency, concurrent load, and human listening were not verified.

## Earlier evidence

[The baseline runtime](verification.md), [host maintenance](host-security.md), [security candidate](security-verification.json), and [public reference test](public-demo-verification.json) describe earlier runs. Some used existing environments or models. They do not override the current fresh-site receipt.
