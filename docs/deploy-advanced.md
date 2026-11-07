# Advanced deployment

Start with [Deploy one site](deploy.md) for the Make commands. This page covers explicit installer flags, trusted HTTPS, prepared hosts, and recovery.

## Explicit installer flags

For automation or a permitted private reference, use `scripts/site.py` directly. `plan` reads price and capacity; `apply` creates one site. This path expects your SSH key and administrator IPv4 addresses explicitly.

```bash
SITE_ARGS=(
  --site retail-ams
  --region nl-ams
  --demo-person
  --ssh-key "$HOME/.ssh/id_ed25519"
  --admin-cidr YOUR_HOME_PUBLIC_IPV4/32
  --admin-cidr YOUR_DEPLOY_COMPUTER_PUBLIC_IPV4/32
)
python3.12 scripts/site.py plan "${SITE_ARGS[@]}"
python3.12 scripts/site.py apply "${SITE_ARGS[@]}" \
  --accept-model-licenses --accept-host-advisories
```

Replace `--demo-person` with `--reference /absolute/private/person.jpg --tenant retailer-a` for your own permitted photo, or omit both for voice shopping only. Keep personal photos outside the source tree. The Make workflow uses the bundled fictional person.

Maintainers testing an unpublished candidate can add `--verify-candidate`. All runtime scans remain active. The host-advisory exception is separate and accepts only documented unfixed host findings.

## Browser access and TLS

The default path needs no domain or certificate. The installer prints a pinned-host SSH tunnel command. Run it from your computer, then open <http://localhost:8765>. Retrieve a retailer key privately from the host's `.local/gateway.env` to sign in.

| Access | Requirement |
| --- | --- |
| Localhost through SSH | No domain or certificate. SSH encrypts the remote connection. |
| Domain with HTTPS | Correct DNS and a valid certificate trusted by the browser. |
| Plain HTTP at the remote IP | Not supported for the browser voice demo. |

Microphone access requires a secure browser context. Localhost qualifies; ordinary remote-IP HTTP does not. A self-signed certificate is not automatically trusted on another computer. See [browser microphone requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia).

For direct HTTPS with restricted ingress, obtain a trusted certificate using DNS validation or your certificate service. Copy the certificate and key to private files on the new host. Keep the key owner-readable only. On that host:

```bash
cd /home/deploy/retail-app
python3.12 scripts/security_tools.py
python3.12 scripts/images.py caddy
python3.12 scripts/gpu.py services --domain YOUR_DNS_NAME \
  --tls-cert /absolute/private/fullchain.pem --tls-key /absolute/private/private.key
sudo install -m 644 .local/systemd/retail-caddy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now retail-caddy
```

Your certificate service must renew these supplied files. Restart Caddy after replacement. This repo does not configure a DNS provider. Public ACME validation is available through `--domain` and `--email`, but requires certificate-authority access that an administrator-only firewall does not provide. Do not open an existing firewall to make validation succeed. See [Caddy certificate validation](https://caddyserver.com/docs/automatic-https#acme-challenges).

## Verify and warm the site

On the GPU host:

```bash
cd /home/deploy/retail-app
set -a
. .local/gateway.env
set +a
.venv/bin/python -m retail_voice.cli preflight
curl --fail http://127.0.0.1:8765/livez
ss -lntp
```

Readiness does not prove natural speech or a warm GPU. Follow [the latency procedure](latency.md), then the stock, preview, cart, and retailer-isolation checks in [Validation](validation.md). Probe private ports from another machine. Verify TLS and both allowed and denied source addresses if you enable HTTPS.

## Prepared host installation

For isolated runtime verification, use a dedicated Ubuntu 24.04 host with Python 3.12, a compatible NVIDIA driver, Docker, NVIDIA Container Toolkit, and the prerequisites listed in `deploy/bootstrap.py`. Use a non-root account with Docker access. The bootstrap itself requires the ownership marker created by `site.py` and refuses unrelated hosts. It installs Ubuntu's supported HWE kernel and matching headers, includes kernel dependencies during upgrades, and disables autoload of unused SMB, NVMe target, and TIPC modules. Reboot and verify `uname -r` and `nvidia-smi` before starting the workers. [The host review](host-security.md) records unresolved vendor advisories.

```bash
python3.12 scripts/demo.py setup
python3.12 scripts/gpu.py configure --site gpu-1 --region YOUR_LINODE_REGION --demo-person
python3.12 scripts/gpu.py install --accept-model-licenses --accept-host-advisories
python3.12 scripts/gpu.py services
```

Replace `--demo-person` with `--reference` and `--tenant` for a private reference, or omit both person options for voice shopping only. Installation verifies hashed dependency locks, pinned model revisions, and [reviewed speech compatibility changes](../deploy/runtime-compatibility.md). It records an immutable model image ID only after the container scan passes. Review `.local/systemd/`, then install and enable the generated units. Keep one gateway worker because sessions and the GPU admission queue live in that process.

The installer pins a patched package installer before installing dependencies. Repeated runs reuse and rescan an unchanged image so its compiler cache remains available. Model downloads and first-start compilation can take many minutes. Service readiness allows up to 30 minutes for initial compilation. This is startup time, separate from response latency after warmup. Private compiler caches are stored by image ID under `.local/runtime/` and reused after restarts. Inspect `journalctl -u retail-vlm -u retail-speech -u retail-gateway` if readiness fails. The speech worker derives a private inference codec without modifying the pinned source checkpoint.

## Stop and recover

Rotate keys with `python3.12 scripts/demo.py rotate-keys`, then restart the gateway. Stop the core stack with `sudo systemctl disable --now retail-gateway retail-speech retail-vlm`; include `retail-tryon` and `retail-caddy` if installed. Resource deletion is separate and must target only the IDs in the site's private state.

Follow [quarantine recovery](troubleshooting.md) after interrupted inference. Never clear the quarantine marker while old GPU work may still be running. Preserve the last working source, configuration, and receipts before updating a site.
