# Ubuntu host remediation

This page records the earlier host-maintenance run. [Fresh-site verification](fresh-site-verification.md) contains the current demo decision, full deployment results, and host assessment.

On November 6, 2026, the isolated Amsterdam test GPU was updated and rebooted into Ubuntu's supported HWE kernel, `7.0.0-42-generic`. NVIDIA driver `620.70.06` loaded successfully. All four demo services recovered, and all 23 real voice-shopping turns passed. The repository checks passed, including 290 Python tests and the new bootstrap regressions. This verifies the existing test runtime after host maintenance, not a fresh cloud deployment. [Measured results](host-security-verification.json).

Available package updates were applied. Obsolete kernels `6.8.0-134` and `6.8.0-147` were removed; `6.8.0-149` remains as a fallback. APT reported zero pending upgrades. The bootstrap now includes new kernel dependencies during upgrades and installs matching HWE headers before the NVIDIA driver. Ubuntu documents this [supported server kernel path](https://ubuntu.com/kernel/lifecycle).

| Scan scope | Before | After |
| --- | ---: | ---: |
| Unique advisories matched to active kernel modules | 3,208 | 612 |
| Critical matches in that scope | 15 | 4 |
| High matches in that scope | 370 | 59 |

The complete post-update scan still reports 3,546 unique advisory IDs across all installed packages, including headers, tools, and the fallback kernel. None has a listed fixed version in the scan. These are package matches, not counts of confirmed exploitable vulnerabilities. No findings were suppressed.

The unused `ksmbd`, `nvmet`, and `tipc` modules were absent from the running kernel. Their autoload restrictions were verified and included in the boot images. The bootstrap applies the same restrictions on a new dedicated host and refuses to disable an in-use module. These controls mitigate the unused services associated with CVE-2026-90162, CVE-2026-98725, and CVE-2026-98805; they do not patch the installed packages or prevent a privileged administrator from bypassing module policy.

SSH's effective `PermitTunnel no` setting mitigates the tunnel-device forwarding issue in [CVE-2026-110928](https://ubuntu.com/security/CVE-2026-110928). Root and password SSH login remain disabled. All existing cloud firewalls and the administrator-only host firewall were preserved.

## Findings at this earlier checkpoint

The installer currently rejects every host advisory match. That strict rule includes inactive packages and findings whose applicability needs review. A scan count alone is insufficient to establish exploitability.

There is also a concrete unresolved issue: Ubuntu identifies [CVE-2026-98432](https://ubuntu.com/security/CVE-2026-98432) as a critical IPv4 TCP vulnerability and lists its HWE fix as work in progress in `7.0.0-43.43~24.04.1`. The configured repositories offer `7.0.0-42`. Ubuntu's packet-filter mitigation is explicitly incomplete and would require firewall changes, which were not authorized. The remaining high-severity findings also require applicability review.

The release gate stays closed. Once the vendor fix is available, install it, reboot, rerun `python scripts/security.py host`, and repeat the GPU checks. Review residual findings against the running configuration. Do not remove the gate or claim zero vulnerabilities merely because package updates are exhausted.
