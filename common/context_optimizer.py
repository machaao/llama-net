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

    Flow:
        1. Read model's native context from GGUF metadata
        2. Apply Ollama's VRAM-tier cap (<24G→4k, 24-48G→32k, >48G→256k)
        3. Memory-aware clamp: reduce if model weights + KV cache exceeds budget
        4. Final fallback: 4096

    Returns:
        ``(n_ctx, auto_detected)`` — the context size and whether it was auto-detected.
    """
    if not model_path or not os.path.exists(model_path):
        logger.warning(f"Cannot calculate context: path missing ({model_path})")
        return 4096, False

    try:
        # 1. Read native context from GGUF
        native_ctx = get_model_context_length(model_path)
        if not native_ctx or native_ctx <= 0:
            logger.info(
                f"No context_length in GGUF for {os.path.basename(model_path)}, "
                f"using VRAM defaults"
            )
            return get_vram_context_cap(), False

        # 2. Architecture info for KV estimation
        arch_info = get_model_architecture_info(model_path)
        n_layers = arch_info.get("n_layers", 32)
        n_embd = arch_info.get("n_embd", 4096)
        n_head = arch_info.get("n_head", 32)
        n_kv_heads = arch_info.get("n_kv_heads", 0)

        # 3. Calculate model size early (needed for VRAM cap)
        try:
            model_gb = os.path.getsize(model_path) / (1024 ** 3)
        except OSError:
            model_gb = 5.0

        # 4. VRAM tier cap — now model-size-aware and concurrency-aware
        vram_cap = get_vram_context_cap(
            model_size_gb=model_gb,
            max_concurrent=max_concurrent,
        )
        effective_ctx = min(native_ctx, vram_cap)

        # 5. Memory-aware clamp
        specs = get_system_specs()
        usable_gb = specs["usable_memory_gb"]

        compute_buf = estimate_compute_buffer_gb(model_gb)

        kv_gb = estimate_kv_cache_gb(
            n_layers=n_layers,
            n_embd=n_embd,
            n_ctx=effective_ctx,
            cache_type=cache_type_k,
            n_head=n_head,
            n_kv_heads=n_kv_heads,
        )

        total_needed = model_gb + kv_gb + compute_buf

        if total_needed <= usable_gb:
            logger.info(
                f"✅ Auto-detected context: {effective_ctx} tokens "
                f"(native={native_ctx}, VRAM cap={vram_cap}, "
                f"KV: {kv_gb:.1f} GB, compute: {compute_buf:.1f} GB, "
                f"total: {total_needed:.1f}/{usable_gb:.1f} GB)"
            )
            return effective_ctx, True

        # Clamp to fit memory
        if n_layers > 0 and n_embd > 0:
            bytes_per_elem = {
                "f16": 2.0, "q8_0": 1.0, "q4_0": 0.5625,
            }.get(cache_type_k, 2.0)
            head_dim = n_embd // n_head if n_head > 0 else 128
            effective_heads = n_kv_heads if n_kv_heads > 0 else n_head
            budget_gb = usable_gb - model_gb - compute_buf
            max_ctx = int(
                (budget_gb * (1024 ** 3))
                / (2 * n_layers * head_dim * effective_heads * bytes_per_elem)
            )
            clamped = 1
            while clamped * 2 <= max_ctx:
                clamped *= 2
            clamped = max(4096, min(clamped, effective_ctx))
            logger.warning(
                f"⚠️ Context {effective_ctx} requires {total_needed:.1f} GB "
                f"but only {usable_gb:.1f} GB usable. Clamped to {clamped} tokens."
            )
            return clamped, True

        logger.warning("Could not estimate memory for clamping, using 4096")
        return 4096, False

    except Exception as e:
        logger.warning(f"Context auto-detection failed for {model_path}: {e}")
        return 4096, False
