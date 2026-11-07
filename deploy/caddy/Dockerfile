FROM caddy@sha256:3db11aead97f1fdff9c563d0dd8e7fba2e4c7031bef40f3e44fd1982f3e8366b AS builder
ENV CGO_ENABLED=0
RUN xcaddy build v2.11.9 \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db \
    --replace github.com/smallstep/certificates=github.com/smallstep/certificates@6d5a251fbe010383c93ead1ab451a329986b80ca \
    --replace go.step.sm/crypto=go.step.sm/crypto@v0.92.0 \
    --replace golang.org/x/crypto=golang.org/x/crypto@v0.58.1-0.20261009181718-2f1f834330b2 \
    --replace golang.org/x/net=golang.org/x/net@v0.62.0

FROM scratch
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
ENTRYPOINT ["/usr/bin/caddy"]
CMD ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
