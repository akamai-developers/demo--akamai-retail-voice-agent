# Fresh Amsterdam verification

The single-site deployment completed on a newly created Amsterdam GPU on November 7, 2026. Voice, stock checks, live try-on, cart continuity, browser behavior, deployment resume, and reboot recovery passed. This candidate is ready for review as a restricted developer demo. Nothing was pushed. [Machine-readable receipt](fresh-site-verification.json).

## What was tested

The actual `scripts/site.py apply` path created one GPU and one firewall, bootstrapped Ubuntu 24.04, installed the driver and container tools, downloaded the models, built fresh GPU environments, built and scanned the model image, and started all four services. It used a 96 GB NVIDIA RTX PRO 6000 Blackwell in `nl-ams`. Models and environments were not copied from an existing deployment.

Testing found and fixed an outdated package installer, readiness script execution and sequencing, image rebuilds on resume, and proxy file permissions. The final deployed code matches the local candidate. Resuming the completed site succeeded without creating duplicates and reused the same immutable model image while scanning it again. First model compilation took about 15 minutes. Warm the models before a presentation.

- All 298 Python tests, formatting, lint, types, JavaScript, audio, transport, and silence-gate checks passed. A fresh source export installed independently and passed package, asset, and authentication checks.
- The reported nine-turn shopping sequence passed all 13 checks. Sold-out yellow medium was neither previewed nor added. Switching to Drizzle Anorak selected navy medium, generated its preview, and added it once without generating another preview.
- Another 50 measured turns passed across GPU loopback and the workstation SSH connection.
- Chromium passed microphone capture, silence stopping, playback, authentication cleanup, retailer isolation, and live preview-to-cart checks. Screenshots were inspected at 1920x1080, 1512x982, 1366x1024, and 390x844.
- Eight synthetic speech read-backs retained the reply content. Word recall ranged from 95% to 100%, with minor recognition substitutions. This is an automatic diagnostic, not a human listening review.
- The try-on reference was the bundled fictional person. Its hash matched the public provenance. The generated navy preview was visually inspected.
- After a host reboot, all four services recovered automatically in 218.3 seconds. Seven additional real voice, preview, and cart turns passed.

## Measured latency

These are warm medians from five samples per step and location. Timing starts at request submission and ends when complete reply audio reaches the client. It excludes speaking, silence detection, and playback. The workload is defined in `tools/measure_latency.py`.

| Step | GPU loopback | Workstation through SSH to AMS |
| --- | ---: | ---: |
| Greeting | 0.91 s | 1.13 s |
| Product search | 2.68 s | 3.17 s |
| Product follow-up | 0.97 s | 1.32 s |
| Stock check | 1.99 s | 2.47 s |
| Spoken request | 1.43 s | 1.94 s |

The separate browser microphone sample took 1.25 seconds from recording completion to playback. Silence detection added about 0.80 seconds before recording completed. That single browser sample is not directly comparable to the five-trial workload.

The first uncached clothing preview took 17.74 seconds to generate and 19.53 seconds through complete spoken confirmation on the GPU host. These are not San Francisco measurements and do not establish a regional performance comparison.

## Security and isolation

All five hashed dependency locks passed their advisory scans. Installed package-index scans passed for the gateway (84 packages), speech (190), and image worker (119). Repository source and the pinned, patched NeMo source are outside package-index advisory lookup; see [runtime compatibility](../deploy/runtime-compatibility.md). The final model container passed OS and Python scans, and the proxy passed its Go dependency scan and restricted-container configuration validation. No advisory ignore list or severity exclusion was used.

Ubuntu had zero pending package upgrades and no pending reboot. Its host scan still reported 3,548 unique advisories across installed packages, including fallback kernels and headers. Active-kernel packages matched 613 advisories, including 4 critical and 59 high. No finding listed an available fixed version. These are package matches, not verified exploits. The run explicitly used `--accept-host-advisories` for restricted demo use. Findings remain recorded; this is not a vulnerability-free or production-security claim.

Cloud rules and the host firewall allow SSH, HTTP, and HTTPS only from the two operator addresses. Model ports and the gateway bind locally and were unreachable from the workstation through the public interface. SSH was also denied from a non-allowlisted test host. All 11 pre-existing instances and all 33 pre-existing firewalls matched the baseline snapshot. A connection from the home address was not directly tested.

The tested browser path uses a pinned SSH tunnel to localhost. The proxy image was validated without publishing ports; trusted certificate issuance and public HTTPS were not tested. Ubuntu 26.04, smaller GPUs, concurrent load, San Francisco latency, and human voice listening remain outside this verification.
