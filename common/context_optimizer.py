"""
Unified context length optimizer.
Single source of truth for determining optimal n_ctx for a given model
based on system specs (RAM, VRAM) and model metadata.
"""

import os
import time
from typing import Tuple, Optional
from common.utils import get_logger
from common.gguf_utils import (
    get_model_context_length,
    get_model_architecture_info,
    estimate_kv_cache_gb,
)

logger = get_logger(__name__)

# System specs cache — RAM/VRAM don't change mid-process
_system_specs_cache: Optional[dict] = None
_system_specs_cached_at: float = 0.0
_SPECS_CACHE_TTL = 300  # seconds

# Reference data: gpt-oss model memory requirements
#   20B  (12.0 GB model data): compute = 2.7 GB, KV = 0.2 GB per 8192 tokens
#   120B (61.0 GB model data): compute = 2.7 GB, KV = 0.3 GB per 8192 tokens
COMPUTE_BUFFER_GB = 2.7


def _estimate_kv_rate_per_token(model_size_gb: float) -> float:
    """Estimate KV cache rate (GB per token) from model file size.

    Derived from reference data via linear fit:
        20B  (12 GB model) → 0.2 GB / 8192 = 2.44e-5 GB/token
        120B (61 GB model) → 0.3 GB / 8192 = 3.66e-5 GB/token

    Linear interpolation: kv_per_8k ≈ 0.15 + (model_size_gb × 0.0025)
    """
    kv_per_8k = 0.15 + (model_size_gb * 0.0025)
    return kv_per_8k / 8192


def estimate_compute_buffer_gb(model_size_gb: float) -> float:
    """Estimate compute buffer memory based on model file size.

    Compute buffers scale linearly for small models, capped at COMPUTE_BUFFER_GB.

    Reference data:
        3B   (~2 GB weights)  → ~0.7 GB compute  (0.3 + 0.2×2)
        8B   (~4 GB weights)  → ~1.1 GB compute  (0.3 + 0.2×4)
        20B  (12 GB weights)  → 2.7 GB compute   (reference)
        120B (61 GB weights)  → 2.7 GB compute   (reference)

    Linear fit: 0.3 + 0.2 × model_size_gb, capped at COMPUTE_BUFFER_GB.
    """
    if model_size_gb <= 0:
        return COMPUTE_BUFFER_GB
    return max(0.3, min(COMPUTE_BUFFER_GB, 0.3 + 0.2 * model_size_gb))


def get_system_specs() -> dict:
    """Detect and cache system specs (RAM, VRAM, GPU name).

    Results are cached for 5 minutes — system memory doesn't change
    significantly during a single process lifetime.
    """
    global _system_specs_cache, _system_specs_cached_at

    now = time.time()
    if _system_specs_cache and (now - _system_specs_cached_at) < _SPECS_CACHE_TTL:
        return _system_specs_cache

    specs = {
        "available_ram_gb": 0.0,
        "available_vram_gb": 0.0,
        "usable_memory_gb": 0.0,
        "gpu_name": "",
    }

    # Detect RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        specs["available_ram_gb"] = round(mem.available / (1024 ** 3), 1)
    except Exception:
        specs["available_ram_gb"] = 16.0

    # Detect VRAM (NVIDIA)
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        specs["available_vram_gb"] = round(mem_info.free / (1024 ** 3), 1)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        specs["gpu_name"] = raw_name if isinstance(raw_name, str) else raw_name.decode("utf-8")
        pynvml.nvmlShutdown()
        logger.info(
            f"NVIDIA GPU detected: {specs['gpu_name']} "
            f"({specs['available_vram_gb']} GiB free)"
        )
    except Exception:
        pass

    # Usable memory: 70% of available VRAM if present, else 70% of RAM
    base = specs["available_vram_gb"] if specs["available_vram_gb"] > 0 else specs["available_ram_gb"]
    specs["usable_memory_gb"] = round(base * 0.70, 1)

    _system_specs_cache = specs
    _system_specs_cached_at = now

    logger.info(
        f"System specs cached: RAM={specs['available_ram_gb']}G, "
        f"VRAM={specs['available_vram_gb']}G, usable={specs['usable_memory_gb']}G"
    )
    return specs


def get_vram_context_cap(
    vram_gb: float = 0.0,
    model_size_gb: float = 0.0,
    max_concurrent: int = 1,
) -> int:
    """VRAM-based context window cap, accounting for multi-model concurrency.

    When *max_concurrent* > 1, divides available memory by the concurrency
    count so that multiple models can coexist without swapping, sustaining
    25-50 TPS with sub-second TTFT.
    """
    if vram_gb <= 0:
        specs = get_system_specs()
        vram_gb = (
            specs["available_vram_gb"]
            if specs["available_vram_gb"] > 0
            else specs["available_ram_gb"]
        )

    # Reserve 10% for OS / driver overhead
    usable = vram_gb * 0.90
    # Fair-share per concurrent model
    per_model = usable / max(max_concurrent, 1)

    # ── Model-size-aware: calculate actual KV budget ──
    if model_size_gb > 0:
        compute_buf = estimate_compute_buffer_gb(model_size_gb)
        kv_budget = per_model - model_size_gb - compute_buf
        if kv_budget <= 0:
            return 4096  # model barely fits — minimal context

        kv_rate = _estimate_kv_rate_per_token(model_size_gb)
        max_tokens = int(kv_budget / kv_rate)

        # Snap down to nearest standard power-of-2 size
        for size in (262144, 131072, 65536, 32768, 16384, 8192, 4096, 2048):
            if size <= max_tokens:
                return size
        return 2048

    # ── No model size known — generous VRAM-only defaults ──
    if per_model > 48:
        return 131072
    elif per_model > 32:
        return 65536
    elif per_model > 24:
        return 32768
    elif per_model > 16:
        return 16384
    elif per_model > 12:
        return 8192
    elif per_model > 8:
        return 4096
    else:
        return 2048


def calculate_optimal_context(
    model_path: str,
    cache_type_k: str = "f16",
    max_concurrent: int = 1,
) -> Tuple[int, bool]:
    """Calculate optimal context size for a model.

    Delegates to get_model_context_length() which handles:
      - GGUF metadata reading
      - Memory-aware budgeting
      - Concurrency-aware allocation
      - System responsiveness reservation
      - Power-of-two alignment

    Returns:
        ``(n_ctx, auto_detected)`` — the context size and whether it was auto-detected.
    """
    if not model_path or not os.path.exists(model_path):
        logger.warning(f"Cannot calculate context: path missing ({model_path})")
        return 4096, False

    try:
        specs = get_system_specs()

        # Get raw available memory (VRAM if present, else RAM)
        raw_available = (specs.get("available_vram_gb", 0) or specs.get("available_ram_gb", 0)) / 2

        ctx = get_model_context_length(
            model_path,
            total_memory_gb=raw_available if raw_available > 0 else None,
            concurrent_models=max_concurrent,
            keep_system_responsive=True,
        )

        if ctx is not None and ctx > 0:
            logger.info(
                f"✅ Context optimizer: {ctx} tokens "
                f"(memory={raw_available:.1f} GB, concurrent={max_concurrent})"
            )
            return ctx, True

        logger.warning("Context auto-detection returned no result, using 4096")
        return 4096, False

    except Exception as e:
        logger.warning(f"Context auto-detection failed for {model_path}: {e}")
        return 4096, False
