# Repository verification

The source was imported into this repository with its existing history and Apache-2.0 license preserved. Private configuration, personal photos, recordings, model weights, and the previous workspace history were excluded.

## Passed locally

- Fresh environment installation and dependency consistency checks.
- 262 Python tests, formatting, lint, type checks, and JavaScript client/audio/transport checks.
- Storefront, prepared try-on, and architecture browser suites at the four documented viewport sizes.
- Wheel installation and bundled-asset smoke checks.
- Source and existing-history secret scans with no findings.
- Gateway dependency audit with no reported advisories.

The latency summary now excludes failed requests from performance medians and reports their count. Regression tests cover fast failures and runs with no successful responses. No mock timing is presented as GPU performance.

## Pending hardware and security work

The available Amsterdam host has an existing deployment. Its current firewall does not allow public SSH, and an approved private management connection was not available from the verification workstation. No remote service was started, stopped, or reconfigured. Existing firewall rules and associations matched the saved baseline after the checks.

Real speech inference, live try-on generation, GPU memory use, and client-to-audio latency have not been measured for this repository on that host. They require an approved management route and confirmation that the test has enough free GPU capacity without interrupting existing workloads.

The GPU dependency findings and NeMo/Lightning compatibility conflict in [Validation](validation.md) remain open. Passing local tests and the gateway audit does not clear those GPU environments or their container packages for release.

Use [the latency procedure](latency.md) after hardware access and dependency compatibility are resolved. Keep raw recordings, host addresses, credentials, and operational snapshots in private storage. No deployment or firewall changes are part of these repository checks.
