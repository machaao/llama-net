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


def get_vram_context_cap(vram_gb: float = 0.0) -> int:
    """Ollama's VRAM-based context window cap.

    Returns the maximum context size based on available GPU/VRAM:
        < 24 GiB  →  4,096 tokens
        24-48 GiB → 32,768 tokens
        > 48 GiB  → 262,144 tokens

    If *vram_gb* is not provided, reads from cached system specs.
    """
    if vram_gb <= 0:
        specs = get_system_specs()
        vram_gb = (
            specs["available_vram_gb"]
            if specs["available_vram_gb"] > 0
            else specs["available_ram_gb"]
        )

    if vram_gb > 48:
        return 262144
    elif vram_gb >= 24:
        return 32768
    else:
        return 4096


def calculate_optimal_context(
    model_path: str,
    cache_type_k: str = "f16",
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

        # 3. VRAM tier cap
        vram_cap = get_vram_context_cap()
        effective_ctx = min(native_ctx, vram_cap)

        # 4. Memory-aware clamp
        specs = get_system_specs()
        usable_gb = specs["usable_memory_gb"]

        kv_gb = estimate_kv_cache_gb(
            n_layers=n_layers,
            n_embd=n_embd,
            n_ctx=effective_ctx,
            cache_type=cache_type_k,
            n_head=n_head,
            n_kv_heads=n_kv_heads,
        )

        try:
            model_gb = os.path.getsize(model_path) / (1024 ** 3)
        except OSError:
            model_gb = 5.0

        total_needed = model_gb + kv_gb + 0.5

        if total_needed <= usable_gb:
            logger.info(
                f"✅ Auto-detected context: {effective_ctx} tokens "
                f"(native={native_ctx}, VRAM cap={vram_cap}, "
                f"KV: {kv_gb:.1f} GB, total: {total_needed:.1f}/{usable_gb:.1f} GB)"
            )
            return effective_ctx, True

        # Clamp to fit memory
        if n_layers > 0 and n_embd > 0:
            bytes_per_elem = {
                "f16": 2.0, "q8_0": 1.0, "q4_0": 0.5625,
            }.get(cache_type_k, 2.0)
            head_dim = n_embd // n_head if n_head > 0 else 128
            effective_heads = n_kv_heads if n_kv_heads > 0 else n_head
            budget_gb = usable_gb - model_gb - 0.5
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
