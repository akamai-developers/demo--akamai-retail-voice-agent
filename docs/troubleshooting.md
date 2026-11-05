# Troubleshooting

| Symptom | Check |
| --- | --- |
| Sign-in fails | Use a retailer key from `.local/gateway.env`, not the operator key. Restart the gateway after key rotation. |
| Microphone unavailable | Use localhost through an SSH tunnel or a valid HTTPS origin. Check browser microphone permissions. |
| Only a tone plays | The gateway is in mock mode. Configure and start the GPU services for real speech. |
| Installation cannot build a package | Confirm Python 3.12 development headers, build-essential, cmake, pkg-config, and libsndfile1 are installed. |
| Gateway will not start | Read service logs and run preflight with `.local/gateway.env` loaded. Confirm all workers use the same GPU_CONFIG. |
| CUDA out of memory | Stop work and inspect `nvidia-smi`. The complete stack is not validated on smaller GPUs. Do not silently switch models or reduce limits without retesting. |
| Try-on unavailable | Confirm TRYON_MODE=live, the configured retailer, a readable reference photo, and `/health` on loopback 8083. |
| Session slot held | End the previous browser conversation or wait for its idle expiration. Each retailer has one retained session. |
| Container name already used | Inspect `docker ps -a`. Stop the old deployment before starting these services; the installer does not delete existing containers. |
| Certificate cannot be issued | Check DNS, inbound TCP 80, IPv6 records, and Caddy logs. |
| Backend quarantined | Stop the gateway. Restart model workers and confirm their previous work ended. Remove only the configured quarantine marker, restart the gateway, and repeat preflight. |

Use `journalctl -u retail-gateway -u retail-speech -u retail-vlm` for startup failures. Add `-u retail-tryon` if enabled. Redact host addresses, credentials, media, transcripts, and personal paths before sharing logs.
