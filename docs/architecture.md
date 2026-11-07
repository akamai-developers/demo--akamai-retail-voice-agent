# Architecture

Each deployment is one independent site in a chosen region: one GPU host with a gateway, a vision-language server, a speech worker, and an optional image worker. Caddy provides HTTPS when a domain is configured. The browser uses the gateway's origin for all requests.

| Component | Work | Listener |
| --- | --- | --- |
| Cloud Firewall | Restricts inbound sources and ports | Configured in your cloud account |
| Caddy | TLS, request limits, proxy | Public TCP 80 and 443 |
| FastAPI and Pipecat | Auth, session state, request sequencing, tool validation | Loopback 8765 |
| vLLM | Nemotron vision and language inference | Loopback 8081 |
| NeMo | Speech recognition and speech synthesis | Loopback 8082 |
| Diffusers | Optional FLUX clothing preview | Loopback 8083 |
| Catalog repository | Synthetic products, stock, prices | In-process |

A spoken turn follows browser capture, gateway admission, speech recognition, language inference and catalog tools, speech synthesis, and browser playback. Text requests skip recognition. The gateway commits selection and cart state only after a successful turn. Exact stock and cart confirmations come from validated tool results.

Try-on uses the selected product and an operator-supplied reference photo. It first resolves an in-stock variant, then calls the image worker through the same inference boundary. Its selection remains available to the next add-to-cart request.

Two retailer identities have separate catalog and session access. They share the same GPU. One turn executes at a time, with one waiting turn. This is logical tenant isolation, not separate GPU or hardware isolation. Carts and session media are held in memory and disappear when a session ends or the gateway restarts.

The app includes an architecture presentation and a live turn view. Presentation animation is explanatory. Live timings come from observed request events. Functions and automatic site selection are deferred to a separate repository. Adding a region means deploying another independent site; this application does not select among sites or synchronize their sessions. Automatic failover, BlueField offload, Guardicore policies, checkout, and persistent carts are outside this implementation.

Branding follows the Akamai Cloud demo template from `akamai/demos/ugv-rover-demo/TEMPLATE.md`, with the approved light storefront theme, bundled logo assets, Instrument Sans, and JetBrains Mono. The template's multi-GPU dashboard, Terraform provisioning, gallery, and authentication failure throttling are not implemented. Caddy provides request rate limits when enabled.
