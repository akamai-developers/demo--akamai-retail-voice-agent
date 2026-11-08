# Deploy one site

Run these commands from the cloned repo on your Mac or Linux computer. The commands create one dedicated GPU and one firewall. They keep the resource IDs, SSH identity, logs, and access key under `.local/sites/SITE_NAME/`.

## 1. Set up

You need Python 3.12, `make`, `ssh`, `scp`, `ssh-keygen`, `curl`, and a Linode API token with Linodes and Firewalls read/write access. The GPU host is set up automatically.

```bash
read -r -s LINODE_TOKEN
export LINODE_TOKEN
make env
```

Paste your token at the silent prompt. It stays in the shell environment. Do not put it in `.env`.

Review `.env`:

| Setting | Default | Change it for |
| --- | --- | --- |
| `SITE_NAME` | `retail-demo` | A separate deployment |
| `LINODE_REGION` | `nl-ams` | Another region with capacity |
| `ADMIN_CIDR` | Empty | Additional administrator addresses |
| `LOCAL_PORT` | `8766` | A different browser port on your computer |
| `GPU_TYPE` | `g3-gpu-rtxpro6000-blackwell-1` | Explicit plan; only this single RTX PRO plan is supported |
| `MODEL_PROFILE` | `retail-full` | The pinned model stack |
| `VLLM_MAX_MODEL_LEN` | `16384` | Context tuning for a new site; range 2048-32768 |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.70` | Total vLLM GPU budget; range 0.40-0.85 |
| `TRYON_ENABLED` | `true` | `false` creates a voice-shopping site without image generation |

Keep the default model and memory settings for the recorded configuration. The accepted tuning ranges do not guarantee that every combination fits. [Models and GPU sizing](gpu-sizing.md) explains model footprints, KV cache, and measurements. Existing `.env` files inherit defaults for omitted settings. Saved GPU/runtime settings cannot change on resume; use a new site name for an experiment.

An empty `ADMIN_CIDR` uses this computer's detected public IPv4 address. For two computers, enter both public IPv4 addresses with `/32`, separated by commas. The list must include the computer running deployment. Both cloud and host firewalls allow TCP 22, 80, and 443 only from that list. Private application and model ports remain closed externally; IPv6 ingress is denied.

## 2. Check capacity and deploy

```bash
make gpu-check
make deploy
```

The capacity table shows current regional prices and availability for `GPU_TYPE`, plus the selected runtime settings. Unsupported plans are rejected before provisioning. If Amsterdam has no capacity, choose another available region in `.env`.

Deployment shows the selected site, hourly price, and allowed addresses. Type `deploy` to proceed. This also accepts the model licenses in [Third-party notices](../THIRD_PARTY_NOTICES.md) and the restricted-demo host exception in [Security](../SECURITY.md). Available host fixes, missing scan coverage, and dependency or container findings still stop installation.

The command creates a private SSH key, provisions the firewall and GPU, installs Ubuntu updates and the driver, reboots, downloads the models, builds scanned images, and starts the services. It waits for readiness and warms speech and reasoning with a synthetic request. Try-on uses the bundled fictional person when enabled.

Plan for **45-90 minutes for a first deployment**. [Unverified: Hardware Run Pending] This total is a planning estimate, not an end-to-end measurement. Download speed, builds, and security scans can extend it. First compilation alone took about 15 minutes in the recorded hardware run. Resuming skips completed steps, so the initial estimate does not apply to a resume.

The terminal shows the GPU's public IPv4 as soon as it is assigned, each installation stage, and elapsed time every 30 seconds. When warmup succeeds, the summary shows the actual duration for that invocation, browser URL, and app API-key location. Detailed output stays in `.local/sites/SITE_NAME/deploy.log`. Measured duration is saved in `.local/sites/SITE_NAME/deployment-timing.json`; it excludes confirmation time and previous attempts.

An interrupted run resumes with the same command and settings; it does not create another GPU. Use `make status` at any time to see saved connection details and the last successful duration. It reads local files without contacting the GPU or requiring a token, so use `make cloud-check` for live readiness.

## 3. Open the app

```bash
make tunnel
```

Run the tunnel from the same computer and clone used for deployment, where the private SSH state is saved. Leave this terminal open. The cloud browser address is <http://localhost:8766>. The local mock uses port 8765, so it can remain running.

In a second terminal in the same clone:

```bash
make key
```

On a Mac, the app API key is copied to your clipboard. On Linux, `make key` displays the key in this terminal. Paste it into the app's sign-in form. It is saved in `.local/sites/SITE_NAME/shopper-key.txt` with owner-only permissions. This is the cloud app key, separate from your `LINODE_TOKEN` and the local mock key in `.local/gateway.env`. Deployment logs and `make status` show the key's location, never its value.

No domain or certificate is needed for this SSH connection. A domain needs a certificate trusted by the browser for direct HTTPS. The microphone does not work over ordinary HTTP at the server IP. [Advanced deployment](deploy-advanced.md#browser-access-and-tls) covers trusted certificates without broadening the firewall.

## 4. Verify

```bash
make cloud-check
make smoke
```

These commands use SSH directly; they do not depend on the browser tunnel. `cloud-check` checks pinned models, GPU visibility, worker readiness, authentication, and separate retailer catalogs. `smoke` uses a synthetic spoken request, generates a live preview, adds the previewed navy medium once, and rejects a sold-out yellow medium. It closes its own sessions. Run it while the demo is idle.

For `TRYON_ENABLED=false`, the smoke check selects navy medium without generating an image, adds it once, and rejects the sold-out variant. The receipt explicitly records the skipped preview.

Receipts are saved on the GPU under `/home/deploy/retail-app/.local/verification/`. Automatic checks do not judge natural voice delivery. Use the browser for the final presentation rehearsal. See [Validation](validation.md) and [Latency](latency.md).

## 5. Delete the site

```bash
make destroy
```

Type the full instance label shown by the command. It verifies ownership, deletes the saved GPU and its local disks, then removes its dedicated firewall and confirms both IDs are absent. It refuses foreign resources, shared firewalls, and unexpected attached volumes, load balancers, or reserved IPs.

Private deletion receipts and SSH keys remain locally. You can deploy the same site name again after a successful deletion. Do not delete `.local/sites/` while resources exist; those files establish ownership and pinned SSH access. Closing the browser, stopping services, or ending the tunnel does not end cloud billing.

## Another region

Set a new `SITE_NAME` and `LINODE_REGION` in `.env`, then run `make gpu-check` and `make deploy`. Each site is independent. To manage the original site again, restore its name and region in `.env`. Existing site configuration and firewalls are not rewritten to accommodate a different region or IP address.

## When a step fails

| Message or symptom | Next step |
| --- | --- |
| `Set LINODE_TOKEN privately` | Read and export the token in this terminal. |
| No capacity in the region | `make gpu-check`, then choose an available region. |
| Add this computer to `ADMIN_CIDR` | Include its detected IPv4 `/32` before creating a new site. |
| Installation stopped | Read the printed `deploy.log`, fix the reported issue, then `make deploy`. |
| Models still loading or a worker failed | `make gpu-logs`. Initial compilation can take many minutes. |
| Local port already in use | Change `LOCAL_PORT` in `.env`, then `make tunnel`. |
| SSH stopped working after moving networks | Use an address already allowed by that site's firewall. Deployment does not silently broaden access. |
| Saved resource or host identity differs | Inspect the recorded IDs in Cloud Manager. Restore original state; do not disable SSH verification. |
| A session is occupied during smoke | End that browser conversation, then rerun `make smoke`. |

For custom references, explicit installer flags, prepared hosts, and HTTPS, use [advanced deployment](deploy-advanced.md). The current Make command tests use fake cloud responses; the [earlier fresh-site run](fresh-site-verification.md) verifies the underlying installer and GPU runtime.
