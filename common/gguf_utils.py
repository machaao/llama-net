"""
GGUF metadata reader using the official gguf package.
Leverages GGUFReader from llama-cpp-python's dependency tree — no extra installs needed.
"""

import os
from typing import Dict, Any, Optional
from common.utils import get_logger

logger = get_logger(__name__)

# In-memory cache for GGUF metadata, keyed by absolute filepath.
# Model files are immutable after download, so no invalidation is needed.
_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}


def _cache_key(filepath: str) -> str:
    """Return absolute path for use as cache key."""
    return os.path.abspath(filepath)


# KV cache bytes per element by quantization type
KV_CACHE_BYTES_PER_ELEMENT = {
    "f16": 2.0,
    "q8_0": 1.0,
    "q4_0": 0.5625,
}

# Map GGUF architecture names to their metadata key prefix
ARCH_PREFIX_MAP: Dict[str, str] = {
    "llama": "llama",
    "mistral": "llama",
    "mistral3": "mistral3",
    "gptoss": "gptoss",
    "qwen2": "qwen2",
    "qwen3": "qwen3",
    "qwen35": "qwen35",
    "phi3": "phi3",
    "gemma": "gemma",
    "gemma2": "gemma2",
    "deepseek2": "deepseek2",
    "command-r": "command-r",
}


def get_arch_prefix(arch: str) -> str:
    """Resolve a GGUF architecture name to its metadata key prefix."""
    return ARCH_PREFIX_MAP.get(arch, arch)


def _read_metadata(filepath: str) -> Dict[str, Any]:
    """Read GGUF metadata using the official GGUFReader API.

    Results are cached in memory by absolute filepath. Model files
    are immutable after download, so the cache never expires.
    """
    key = _cache_key(filepath)
    if key in _METADATA_CACHE:
        return _METADATA_CACHE[key]

    from gguf.gguf_reader import GGUFReader

    reader = GGUFReader(filepath)
    metadata: Dict[str, Any] = {}

    # Handle both dict (older gguf) and list (gguf >= latest) formats
    field_list = list(reader.fields.values()) if isinstance(reader.fields, dict) else list(reader.fields)

    for field in field_list:
        try:
            # Skip tokenizer arrays (131K+ entries) — we don't need them
            # and they're expensive to convert via contents()
            if field.name.startswith("tokenizer.ggml.") and field.name not in (
                "tokenizer.ggml.model",
                "tokenizer.ggml.pre",
            ):
                continue

            # Skip internal GGUF reader fields
            if field.name.startswith("GGUF."):
                continue

            value = field.contents()
            if value is not None:
                metadata[field.name] = value

        except Exception as e:
            logger.debug(f"Could not read GGUF field '{field.name}': {e}")

    _METADATA_CACHE[key] = metadata
    logger.debug(f"Cached GGUF metadata for {os.path.basename(filepath)} ({len(metadata)} fields)")
    return metadata


def get_model_context_length(filepath: str) -> Optional[int]:
    """Get the model's trained context length from GGUF metadata."""
    try:
        meta = _read_metadata(filepath)

        arch = meta.get("general.architecture")
        ctx = meta.get("general.context_length")

        # Fallback: architecture-specific key
        if ctx is None and arch:
            prefix = get_arch_prefix(str(arch))
            ctx = meta.get(f"{prefix}.context_length")

        if ctx is not None:
            ctx = int(ctx)
            if ctx > 0:
                logger.info(f"GGUF context_length: {ctx} (arch: {arch})")
                return ctx

        logger.debug("No context_length found in GGUF metadata")
        return None

    except Exception as e:
        logger.warning(f"Could not read context length from {filepath}: {e}")
        return None


def get_model_architecture_info(filepath: str) -> Dict[str, Any]:
    """Get architecture info needed for memory estimation."""
    default = {
        "architecture": "unknown",
        "context_length": 0,
        "n_layers": 0,
        "n_embd": 0,
        "n_head": 0,
        "n_kv_heads": 0,
    }
    try:
        meta = _read_metadata(filepath)
        arch = str(meta.get("general.architecture", "unknown"))
        prefix = get_arch_prefix(arch)

        def _find_int(*keys: str) -> int:
            for k in keys:
                v = meta.get(k)
                if v is not None:
                    return int(v)
            return 0

        return {
            "architecture": arch,
            "context_length": _find_int(
                "general.context_length",
                f"{prefix}.context_length",
            ),
            "n_layers": _find_int(
                f"{prefix}.block_count",
                f"{prefix}.layer_count",
            ),
            "n_embd": _find_int(
                f"{prefix}.embedding_length",
                f"{prefix}.hidden_size",
            ),
            "n_head": _find_int(
                f"{prefix}.attention.head_count",
                f"{prefix}.num_attention_heads",
            ),
            "n_kv_heads": _find_int(
                f"{prefix}.attention.head_count_kv",
            ),
        }

    except Exception as e:
        logger.debug(f"Could not read architecture info from {filepath}: {e}")
        return default


def estimate_kv_cache_gb(
    n_layers: int,
    n_embd: int,
    n_ctx: int,
    cache_type: str = "f16",
    n_kv_heads: Optional[int] = None,
    n_head: Optional[int] = None,
) -> float:
    """Estimate KV cache memory in GB for a given configuration."""
    if n_layers == 0 or n_embd == 0 or n_ctx == 0:
        return 0.0

    bytes_per_elem = KV_CACHE_BYTES_PER_ELEMENT.get(cache_type, 2.0)
    head_dim = n_embd // n_head if n_head and n_head > 0 else 128

    kv_dim = n_embd
    if n_kv_heads and n_head and n_kv_heads < n_head:
        kv_dim = n_kv_heads * head_dim

    total_bytes = 2 * n_layers * n_ctx * kv_dim * bytes_per_elem
    return round(total_bytes / (1024 ** 3), 2)
