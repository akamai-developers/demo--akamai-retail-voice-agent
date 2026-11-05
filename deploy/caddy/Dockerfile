# Caddy 2.10.2 (the image pinned in profiles/nvidia-one-gpu.json) plus the rate limit plugin,
# built from pinned sources. Standard Caddy ships no rate limiter; this adds github.com/mholt/caddy-ratelimit.
# Build on the GPU host from reference-app/ (host networking: the bridge network has no DNS there):
#   docker build --network host -t retail-caddy:2.10.2-ratelimit deploy/caddy
FROM caddy@sha256:01668408cc26e2e00c9d067c30cb43b2ba14ad1f2808beda55503cb2a31f59dc AS builder
RUN xcaddy build v2.10.2 \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

FROM caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
