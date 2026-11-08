# Models and GPU sizing

Use one `g3-gpu-rtxpro6000-blackwell-1`: a 96 GB RTX PRO 6000 Blackwell. The `retail-full` profile keeps the pinned model stack used in the [Amsterdam hardware run](fresh-site-verification.md). The defaults remain unchanged. Other GPU plans and model profiles are rejected before cloud provisioning; there is no RTX 4000 or multi-GPU profile.

## Model inventory

| Role | Repository ID | Size or purpose |
| --- | --- | --- |
| Speech recognition | `nvidia/nemotron-speech-streaming-en-0.6b` | 0.6B parameters |
| Speech synthesis | `nvidia/magpie_tts_multilingual_357m` | 357M parameters, excluding its separate codec |
| Speech codec | `nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps` | Converts audio tokens to a waveform; included in speech-worker memory |
| Reasoning and vision | `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8` | 30B-class mixture-of-experts model; vendor FP8 footprint of 32.8 GB |
| TTS tokenizer | `google/byt5-small` | Tokenizer/configuration files only; no ByT5 model weights |
| Image generation | `black-forest-labs/FLUX.2-klein-4B` | 4B diffusion transformer plus its text encoder and VAE |

The [speech and reasoning manifest](../src/retail_voice/profiles/nvidia-one-gpu.json) pins revisions. The [image worker model definition](../src/retail_voice/tryon.py) pins FLUX. Model cards and licenses are linked in [Third-party notices](../THIRD_PARTY_NOTICES.md). Model names and parameter counts are not runtime memory measurements.

NVIDIA reports 32.8 GB for the FP8 checkpoint. The active-parameter count describes computation per token; it does not reduce the resident checkpoint to a 3B model. Cache, activations, framework allocations, and other workers need additional memory. [NVIDIA model card](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8).

The image worker loads BF16 weights and uses model CPU offload. It moves components between host RAM and the GPU as needed. The 4B label counts the diffusion transformer, not the entire pipeline, and does not establish peak memory. The configured preview is 768 by 1024 pixels at four inference steps. [FLUX model card](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B).

## Memory budget and KV cache

The recorded GPU exposes 97,887 MiB, about 95.6 GiB. Hardware product names use GB; this arithmetic uses the MiB reported by `nvidia-smi`.

| Allocation | Configured amount | Includes |
| --- | ---: | --- |
| vLLM engine | 70%, about 66.9 GiB | Model weights, runtime allocations, and cache |
| Outside the vLLM budget | 30%, about 28.7 GiB | Speech worker, image-worker GPU use, CUDA overhead, and headroom |

These are budgets, not measured consumption. CPU offload also uses host RAM. Neither disk checkpoint size nor parameter count captures all inference memory.

KV cache stores attention keys and values for retained context. The defaults are `--max-model-len 16384`, `--max-num-seqs 1`, `--kv-cache-dtype fp8`, and `--gpu-memory-utilization 0.70`. The context limit includes prompt and generated tokens. This hybrid model also has non-attention state, so a generic dense-transformer cache formula is not sufficient to size the whole runtime.

vLLM profiles runtime memory and determines cache capacity within its budget. Lowering the budget leaves more room for other workers but can leave too little for the model or context. Increasing context can require more cache. Increasing the budget can starve speech or image generation. The engine's startup report and a complete shopping run must establish the usable cache and headroom. [vLLM engine arguments](https://docs.vllm.ai/en/stable/configuration/engine_args/).

[Unverified: Hardware Run Pending] Exact cache bytes/token capacity, speech-worker peak, image-worker peak, and whole-stack peak VRAM were not captured in the archived run. The default full stack passed functional testing. Do not interpret that result as the minimum GPU size or a concurrency benchmark.

## Settings

Set these in the clone's `.env` before creating a site:

| Setting | Default | Behavior |
| --- | --- | --- |
| `GPU_TYPE` | `g3-gpu-rtxpro6000-blackwell-1` | Used for capacity, pricing, and provisioning. Currently the only supported plan. |
| `MODEL_PROFILE` | `retail-full` | Selects the pinned stack above. Currently the only supported profile. |
| `VLLM_MAX_MODEL_LEN` | `16384` | Integer from 2048 to 32768. Only the default has recorded full-stack results. |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.70` | Fraction from 0.40 to 0.85. These are input bounds, not a guarantee every combination fits. |
| `TRYON_ENABLED` | `true` | `false` omits the image environment, model download, and worker on a new site. Voice, search, stock, and cart remain. |

`make gpu-check` reports the selected plan, region capacity, current prices, and runtime settings. `make deploy` passes the settings to the host installer. `make status` shows the saved deployment settings, even if `.env` has since been edited.

Deployments created before these variables were added retain their original defaults. Resume refuses changes to the saved GPU, model, context, memory budget, or try-on setting. Restore the original values to resume, or choose a new `SITE_NAME` to experiment. `make env` preserves an existing `.env`; omitted settings use the defaults above.

With try-on disabled, use "Show me the Drizzle Anorak in navy medium. Check stock", then "Add it to my cart". `make smoke` checks selection-to-cart continuity and sold-out rejection and records that image generation was skipped. It does not report a live-preview pass.

Lowering the context limit does not shrink the model weights. A 20 GB RTX 4000 Ada cannot hold the current 32.8 GB FP8 checkpoint plus runtime overhead entirely on its GPU. A smaller model needs separate quality, memory, and latency validation. Multiple GPUs also need explicit worker placement or model sharding: this runtime uses GPU 0 with tensor parallelism of one. [Akamai GPU specifications](https://techdocs.akamai.com/cloud-computing/docs/gpu-compute-instances).

## Record memory on a future hardware run

On the dedicated GPU host, save startup logs and memory snapshots privately:

```bash
cd /home/deploy/retail-app
mkdir -p .local/verification
sudo journalctl -u retail-vlm -b --no-pager > .local/verification/vlm-startup.log
nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv
```

Retain vLLM's startup lines reporting model memory, available cache memory, token capacity, and context/concurrency. Sample `nvidia-smi` repeatedly during warm voice requests and an uncached live preview, not only while idle. Record sample interval, per-process and whole-GPU peaks, host RAM, model revisions, context, cache dtype, memory fraction, and workload. Sampling can miss brief peaks; label the result as sampled. Keep raw logs private and publish a sanitized summary with its method.

Run the [latency workload](latency.md) separately after warmup. The README reports medians from five samples per workload and location, plus one uncached preview. Neither a different GPU nor a changed context/memory setting inherits those measurements.

## Storage

Object Storage is not used. Model files, compiler caches, and services live on the GPU instance's local disk. Carts and generated previews live in session memory. The deployment computer retains private SSH state, logs, and receipts. Deleting the GPU removes its local model files; ending a session removes its cart and preview data.
