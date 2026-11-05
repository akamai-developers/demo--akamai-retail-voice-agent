# Security

This is a demonstration application with a synthetic catalog and an in-memory cart. It does not process payments.

Setup creates three distinct random keys in `.local/gateway.env` with owner-only permissions. Retailer keys grant access to one tenant. The operator key controls readiness and draining through loopback or an SSH tunnel. Do not put keys in URLs, screenshots, command arguments, issues, or git.

Keep `.local`, model weights, recordings, generated previews, and personal photos outside version control. A `.gitignore` does not remove files already committed. Release archives are built from an explicit source allowlist, without git history. Scan that archive and the new repository before publishing.

The model workers accept only loopback traffic and do not authenticate local clients. Use a dedicated host with trusted local accounts. The pinned model snapshot contains custom model code; review its license and source before enabling it. Downloading packages and models requires network access. Inference uses local workers.

Before exposing HTTPS, configure both cloud and host firewalls, restrict the audience, and verify private ports from another machine. Caddy request limits complement the gateway's bounded inputs, tenant checks, session limits, and serialized inference. This is not a production identity or abuse prevention system.

Run `python scripts/public_check.py`, Gitleaks, and `pip-audit -r requirements.lock.txt` before publishing. Audit the resolved speech and image environments separately on the GPU host. A passing scan reports known findings at scan time; it does not establish the absence of every secret or vulnerability.

Report security issues privately through the repository's GitHub Security Advisories page when enabled. Do not disclose credentials or exploitable details in a public issue. If private reporting is not enabled, contact the repository owner through an existing private channel.
