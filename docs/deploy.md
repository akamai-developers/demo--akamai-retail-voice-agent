# Deploy to a dedicated GPU host

This installs a single-host demo. It does not create a Linode, select a region automatically, or install a GPU driver. Use a newly allocated host or one dedicated to this application. Do not apply these service or firewall commands to an existing deployment.

The pinned GPU dependencies have unresolved security advisories. Read [Validation](validation.md) before installing; the functional hardware run does not clear these dependencies for public release.

## 1. Prepare the host

Use Ubuntu 24.04, Python 3.12, an NVIDIA driver compatible with CUDA 12.9, Docker Engine, and NVIDIA Container Toolkit. Use a non-root account with access to Docker. Docker access grants control over the host; use a dedicated demo host.

The reference hardware has 96 GB VRAM. Reserve space for the VLM, speech models, image model, CUDA environments, and container images. Check available disk space before starting downloads. A 48 GB VLM minimum does not establish that all services fit together.

Install the operating-system build dependencies:

```bash
sudo apt-get update
sudo apt-get install -y python3.12-venv python3.12-dev build-essential cmake pkg-config git curl libsndfile1 ffmpeg
nvidia-smi
docker info
```

Follow the vendor instructions for [Docker on Ubuntu](https://docs.docker.com/engine/install/ubuntu/) and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Verify a CUDA container can see the GPU before continuing.

Clone this repository onto the GPU host. Use a path without spaces. All remaining commands run from that clone as the non-root service account.

## 2. Configure and install

```bash
python3.12 scripts/demo.py setup
python3.12 scripts/gpu.py configure --site gpu-1 --region YOUR_LINODE_REGION
python3.12 scripts/gpu.py install --accept-model-licenses
```

Replace `YOUR_LINODE_REGION` with the host's region ID. Configuration writes `.local/gpu.json` and private environment files, creates unique credentials, and sets a 16,384-token context limit. `--model-dir /absolute/path` stores weights elsewhere. Rerunning configuration preserves keys.

Read the model licenses linked in [third-party notices](../THIRD_PARTY_NOTICES.md) before passing the acceptance flag. Installation downloads the exact model revisions in `src/retail_voice/profiles/nvidia-one-gpu.json`, creates a separate speech environment, and records its resolved dependencies in `.local/speech-resolved.txt`. It does not start services. The NeMo environment has direct pins, not a complete portable lock; retain and audit the resolved file for each host.

For try-on, provide your own permitted reference photo before installation:

```bash
python3.12 scripts/gpu.py configure --site gpu-1 --region YOUR_LINODE_REGION \
  --reference /absolute/private/path/person.jpg --tenant retailer-a
python3.12 scripts/gpu.py install --accept-model-licenses
```

Use a JPEG or PNG photo under 2 MB and 4 megapixels. Convert WebP or other formats before configuration. It stays on the host and is used only for the configured retailer. The repository includes no person photo. Try-on is a generated appearance preview, not a size or fit prediction. The app checks stock before generating a preview and retains the selected variant when adding it to the cart.

The download includes pinned tokenizer files needed by the speech checkpoint. On its first start, the speech worker derives a private inference codec without training-only components. The pinned source checkpoint stays unchanged. The derived checkpoint and its hash receipt live in `.local/runtime/` and are verified before reuse. Model serving remains offline after installation.

## 3. Start services

```bash
python3.12 scripts/gpu.py services
```

Review `.local/systemd/`. Units contain your account and clone paths, not credential values. Install the three core services:

```bash
sudo install -m 644 .local/systemd/retail-vlm.service .local/systemd/retail-speech.service .local/systemd/retail-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now retail-vlm retail-speech retail-gateway
journalctl -u retail-gateway -f
```

If configured, install the image worker too:

```bash
sudo install -m 644 .local/systemd/retail-tryon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now retail-tryon
```

Services wait up to 20 minutes for initial readiness. Inspect `journalctl -u retail-vlm -u retail-speech -u retail-tryon` for failures. One gateway worker owns the session state and the GPU admission queue. Do not add Uvicorn workers.

From your laptop, create a tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_GPU_HOST
```

Open <http://localhost:8765> and sign in using a retailer key from the host's `.local/gateway.env`. Localhost supports browser microphone access. Use this path for the first functional check. Warm the models using [the latency procedure](latency.md) after each restart. The verified host took about 40 seconds for its first cold greeting despite passing readiness checks.

## 4. Optional public HTTPS

This optional configuration has not been verified on the new Amsterdam host. That host permits SSH, HTTP, and HTTPS only from two operator addresses and uses the tunnel above. Keep those access restrictions when repeating that deployment; the certificate-validation setup below is a different access policy.

Point a DNS A record at the host. Add an AAAA record only if the host and firewall are configured for IPv6. Configure a Linode Cloud Firewall with inbound default DROP, SSH restricted to your administrator CIDR, TCP 80 open for ACME certificate validation, and TCP 443 limited to the audience CIDRs. Allow outbound connections for package/model downloads and certificate renewal. Apply equivalent host firewall rules. Never expose ports 8765, 8081, 8082, or 8083.

Build the pinned Caddy image with request rate limiting, then render the service:

```bash
docker build -t retail-caddy:2.10.2-ratelimit deploy/caddy
python3.12 scripts/gpu.py services --domain YOUR_DNS_NAME --email YOUR_CERTIFICATE_EMAIL
docker run --rm --network none -v "$PWD/.local/Caddyfile:/etc/caddy/Caddyfile:ro" \
  retail-caddy:2.10.2-ratelimit caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo install -m 644 .local/systemd/retail-caddy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now retail-caddy
```

Caddy obtains and renews certificates automatically. It redirects HTTP to HTTPS, limits request size and rate, and blocks operator routes from the public proxy. The public page and `/v1/info` require no key; shopping APIs do. Keep HTTPS restricted to your demo audience.

## 5. Verify the installation

```bash
set -a
. .local/gateway.env
set +a
.venv/bin/python -m retail_voice.cli preflight
curl --fail http://127.0.0.1:8765/livez
ss -lntp
```

Check public and private ports from a separate host, including IPv6 if enabled. Complete the spoken conversation and image checks in [Validation](validation.md). Preflight checks readiness, configuration and receipts; it does not prove speech quality or GPU capacity under load.

## Update, stop, and recover

Before updating, end active conversations and retain the current source version, `.local` configuration, and dependency receipts. Run `scripts/demo.py setup` after a source update, then restart `retail-gateway`. A model revision or GPU configuration change requires reinstalling as needed and restarting vLLM, speech, try-on, and the gateway in that order. Repeat preflight and a real conversation.

To rotate credentials, run `python3.12 scripts/demo.py rotate-keys` and restart `retail-gateway`. Old keys stop working after restart.

To stop the core stack:

```bash
sudo systemctl disable --now retail-gateway retail-speech retail-vlm
```

Stop `retail-caddy` and `retail-tryon` too if installed. Stopping services does not delete models or end the host's cloud charges. Manage or delete the Linode separately.

A failed or interrupted inference can leave `.local/runtime/backend-quarantine.json`. To recover, stop the gateway, restart all model workers, confirm their old work ended and the new workers are ready, then remove only that marker and start the gateway. Never clear it while old CUDA work might still be running. See [Troubleshooting](troubleshooting.md).
