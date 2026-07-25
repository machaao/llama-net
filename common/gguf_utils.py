"""
GGUF metadata reader using the official gguf package.
Leverages GGUFReader from llama-cpp-python's dependency tree — no extra installs needed.
"""

from typing import Dict, Any, Optional
from common.utils import get_logger

logger = get_logger(__name__)

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
    """Read GGUF metadata using the official GGUFReader.

    Handles all GGUF types (strings, scalars, arrays) including
    massive tokenizer arrays (250K+ strings) without issues.

    Uses getattr() for field attributes to handle different
    GGUFReader API versions gracefully.
    """
    from gguf.gguf_reader import GGUFReader

    reader = GGUFReader(filepath)
    metadata: Dict[str, Any] = {}

    for field in reader.fields:
        try:
            # Handle both dict-style and object-style fields
            if isinstance(field, str):
                continue

            name = getattr(field, 'name', None) or getattr(field, 'key', None)
            data = getattr(field, 'data', None) or getattr(field, 'value', None)

            if name is None or data is None:
                logger.debug(f"Skipping field with missing name/data: {type(field)}")
                continue

            if hasattr(data, 'dtype') and data.dtype == 'uint8' and data.ndim == 1:
                # String field stored as raw bytes → decode
                metadata[name] = bytes(data).decode('utf-8', errors='replace')
            elif hasattr(data, '__len__') and len(data) == 1:
                # Scalar field (int, float, bool)
                val = data[0]
                metadata[name] = val.item() if hasattr(val, 'item') else val
            elif hasattr(data, '__len__') and len(data) > 1:
                # Array field
                metadata[name] = [
                    x.item() if hasattr(x, 'item') else x for x in data
                ]
            else:
                # Fallback — try direct use
                metadata[name] = data

        except Exception as e:
            logger.debug(f"Could not read GGUF field: {e}")

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
