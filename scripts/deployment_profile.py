"""Shared, validated settings for the supported single-GPU deployment."""

GPU_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
MODEL_PROFILE = "retail-full"
CONTEXT_TOKENS = 16384
MEMORY_FRACTION = 0.70


def validate_gpu(value):
    if value != GPU_TYPE:
        raise ValueError(
            f"GPU_TYPE={value} is not supported by {MODEL_PROFILE}. "
            f"Use {GPU_TYPE}: one 96 GB RTX PRO 6000 Blackwell. "
            "Smaller and multi-GPU plans need a separately validated profile."
        )


def runtime_settings(profile, context, memory):
    if profile != MODEL_PROFILE:
        raise ValueError(f"MODEL_PROFILE must be {MODEL_PROFILE}; model revisions are pinned")
    if not str(context).isdigit() or not 2048 <= int(context) <= 32768:
        raise ValueError("VLLM_MAX_MODEL_LEN must be an integer from 2048 to 32768")
    try:
        fraction = float(memory)
    except (TypeError, ValueError):
        raise ValueError("VLLM_GPU_MEMORY_UTILIZATION must be from 0.40 to 0.85") from None
    if not 0.40 <= fraction <= 0.85:
        raise ValueError("VLLM_GPU_MEMORY_UTILIZATION must be from 0.40 to 0.85")
    return {
        "model_profile": profile,
        "context_tokens": int(context),
        "gpu_memory_utilization": fraction,
    }


def with_runtime_defaults(config):
    """Older saved sites used these same defaults before they became configurable."""
    return runtime_settings(MODEL_PROFILE, CONTEXT_TOKENS, MEMORY_FRACTION) | config


def add_arguments(parser):
    parser.add_argument("--model-profile", default=MODEL_PROFILE)
    parser.add_argument("--context-tokens", type=int, default=CONTEXT_TOKENS)
    parser.add_argument("--gpu-memory-utilization", type=float, default=MEMORY_FRACTION)


def arguments(config):
    return [
        "--model-profile",
        config["model_profile"],
        "--context-tokens",
        str(config["context_tokens"]),
        "--gpu-memory-utilization",
        str(config["gpu_memory_utilization"]),
    ]
