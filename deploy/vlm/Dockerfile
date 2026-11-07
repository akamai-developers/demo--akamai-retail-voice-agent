FROM cgr.dev/chainguard/wolfi-base@sha256:8509ee4351a2e501b7f98201f2e2642396e82020c61a589cacdcfe74b9de46c1 AS builder
RUN apk update && apk upgrade --no-cache \
    && apk add --no-cache python-3.11 py3.11-pip python-3.11-dev libstdc++ libgomp numactl
COPY vlm-requirements.lock.txt /tmp/requirements.txt
RUN python3.11 -m venv /opt/runtime \
    && /opt/runtime/bin/python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && /opt/runtime/bin/python -m pip check \
    && /opt/runtime/bin/python -m pip uninstall -y pip

FROM cgr.dev/chainguard/wolfi-base@sha256:8509ee4351a2e501b7f98201f2e2642396e82020c61a589cacdcfe74b9de46c1
RUN apk update && apk upgrade --no-cache \
    && apk add --no-cache python-3.11 python-3.11-dev libstdc++ libgomp numactl gcc-15-default openssl
COPY --from=builder /opt/runtime /opt/runtime
COPY link_cuda.py /usr/local/libexec/link_cuda.py
RUN /opt/runtime/bin/python /usr/local/libexec/link_cuda.py
ENV HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
ENV HOME=/tmp HF_HOME=/tmp/huggingface XDG_CACHE_HOME=/tmp/cache
ENV CUDA_HOME=/opt/runtime/lib/python3.11/site-packages/nvidia/cu13
ENV PATH=/opt/runtime/lib/python3.11/site-packages/nvidia/cu13/bin:/opt/runtime/bin:/usr/bin:/bin
ENV LIBRARY_PATH=/opt/runtime/lib/python3.11/site-packages/nvidia/cu13/lib:/usr/lib/x86_64-linux-gnu
ENV LD_LIBRARY_PATH=/opt/runtime/lib/python3.11/site-packages/nvidia/cu13/lib:/usr/lib/x86_64-linux-gnu
USER 65532:65532
ENTRYPOINT ["/opt/runtime/bin/vllm", "serve"]
