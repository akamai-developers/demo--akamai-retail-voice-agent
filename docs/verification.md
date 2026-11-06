# Repository verification

The application passed functional checks on a newly allocated Amsterdam GPU on November 5 and 6, 2026. It is **not cleared for public release**: the installed GPU environments have unresolved dependency advisories. The existing deployments were not modified.

The tested source is commit `8aa9ae6a3a628bd81fddf629fcfd4efbab8e47ea`. The host runs Ubuntu 24.04, Python 3.12, driver 620.70.06, and one RTX PRO 6000 Blackwell Server Edition with 97,887 MiB VRAM. Model revisions, aggregate results, and hashes of private evidence are in [the verification receipt](ams-verification.json).

## Functional results

- 266 Python tests passed, along with formatting, lint, type checks, JavaScript checks, and fresh wheel installation.
- All 50 measured GPU turns completed across ten trials.
- The nine-turn shopping regression passed all eleven checks. Unavailable yellow medium was neither previewed nor added. Switching to Drizzle Anorak worked, and adding after its live preview retained navy medium with quantity one.
- The two-retailer conversation passed runtime and shopping checks. Two tool-call expectations were missed because the model refused foreign-catalog requests directly. No foreign product was returned. Human listening remains pending.
- The live browser suite passed microphone capture, silence detection, playback, cart, session cleanup, and retailer isolation. Screenshots were inspected at 1920x1080, 1512x982, 1366x1024, and 390x844.
- After an operating-system update and reboot, all four services recovered, preflight passed, and five warmup turns completed.
- The generated preview was inspected visually. This does not establish size or fit accuracy.

The fresh-host run exposed missing offline tokenizer assets and a codec loader that requested training-only dependencies. The tested source pins the tokenizer assets and derives a hash-verified inference codec without changing the original checkpoint.

## Measured latency

The client was in Chicago (`us-ord`), connected through an SSH tunnel to Amsterdam (`nl-ams`). These are request-to-complete-audio medians, with ten successful samples per row.

| Request | Median |
| --- | ---: |
| Greeting | 0.93 s |
| Product search | 3.08 s |
| Product follow-up | 1.24 s |
| Stock check | 2.39 s |
| Spoken request | 2.29 s |

For the spoken request, server medians were 32 ms for recognition, 147 ms for language-model work, and 1,356 ms for speech synthesis. Speech synthesis was the largest measured server stage. These measurements exclude speaking time, silence detection, audio-device startup, and playback.

One browser sample took 3.08 seconds from recording end to playback start; its recording contained 801 ms of trailing quiet. This is one observation, not a median. A separate first, uncached try-on took 24.22 seconds for generation and 26.42 seconds until the complete audio reply arrived. Neither result establishes San Francisco latency or a region comparison.

Cold startup is material. Before the measured trials, the first greeting took 40.01 seconds and the first spoken request took 14.82 seconds. Warm the services after startup and before presenting. An observed loaded-memory sample was 74,681 MiB; peak memory under concurrent load was not measured. See [the repeatable measurement procedure](latency.md).

## Security and access

The new host has cloud and host firewalls with inbound default deny. Only the operator's home and verification workstation IPv4 addresses may reach TCP 22, 80, and 443. Inbound IPv6 is denied. SSH requires keys and disables root and password login. All 32 pre-existing cloud firewall rules and associations matched their saved baseline.

The gateway and model APIs bind to loopback. Public-address probes from the allowed workstation could reach SSH but could not reach the gateway, model APIs, or the model worker's internal distributed listener. Unauthenticated preview access was denied. HTTP and HTTPS have no listener on this test host; access uses a local SSH tunnel. Home-client authentication, rejection from a separate disallowed source, and HTTPS were not tested.

The installed gateway's 83-package advisory scan reported no findings. The speech, image, and model-serving scans found affected packages in 4, 4, and 27 packages respectively. These are scanner findings, not proof that every advisory is exploitable through this app. The NeMo/Lightning compatibility conflict remains unresolved; see [Validation](validation.md). Source-level review of pinned dependencies and complete operating-system/container audits remain outside this receipt.

Secret and release-file checks exclude private configuration, personal reference photos, generated personal previews, recordings, weights, operational addresses, and previous workspace history. Private evidence is retained outside the public file set. No source was pushed as part of this verification.
