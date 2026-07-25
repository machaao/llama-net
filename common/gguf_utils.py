"""
GGUF header parser for reading model metadata without loading tensors.
Only reads the file header (~first few KB) — never touches tensor data.
"""

import struct
from typing import Dict, Any, Optional, List
from common.utils import get_logger

logger = get_logger(__name__)

# GGUF value type constants
GGUF_TYPE_UINT8 = 0
GGUF_TYPE_INT8 = 1
GGUF_TYPE_UINT16 = 2
GGUF_TYPE_INT16 = 3
GGUF_TYPE_UINT32 = 4
GGUF_TYPE_INT32 = 5
GGUF_TYPE_FLOAT32 = 6
GGUF_TYPE_BOOL = 7
GGUF_TYPE_STRING = 8
GGUF_TYPE_ARRAY = 9
GGUF_TYPE_UINT64 = 10
GGUF_TYPE_INT64 = 11
GGUF_TYPE_FLOAT64 = 12

GGUF_MAGIC = 0x46475547  # "GGUF" little-endian

# KV cache bytes per element by quantization type
KV_CACHE_BYTES_PER_ELEMENT = {
    "f16": 2.0,
    "q8_0": 1.0,
    "q4_0": 0.5625,
}

# Map GGUF architecture names to their metadata key prefix.
# Models sharing a prefix (e.g. "mistral" → "llama") use the same
# GGUF schema keys for layer/embedding/head counts.
ARCH_PREFIX_MAP: Dict[str, str] = {
    "llama": "llama",
    "mistral": "llama",
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


def _read_string(f) -> str:
    length = struct.unpack('<Q', f.read(8))[0]
    return f.read(length).decode('utf-8')


def _read_value(f, value_type: int) -> Any:
    if value_type == GGUF_TYPE_UINT8:
        return struct.unpack('<B', f.read(1))[0]
    elif value_type == GGUF_TYPE_INT8:
        return struct.unpack('<b', f.read(1))[0]
    elif value_type == GGUF_TYPE_UINT16:
        return struct.unpack('<H', f.read(2))[0]
    elif value_type == GGUF_TYPE_INT16:
        return struct.unpack('<h', f.read(2))[0]
    elif value_type == GGUF_TYPE_UINT32:
        return struct.unpack('<I', f.read(4))[0]
    elif value_type == GGUF_TYPE_INT32:
        return struct.unpack('<i', f.read(4))[0]
    elif value_type == GGUF_TYPE_UINT64:
        return struct.unpack('<Q', f.read(8))[0]
    elif value_type == GGUF_TYPE_INT64:
        return struct.unpack('<q', f.read(8))[0]
    elif value_type == GGUF_TYPE_FLOAT32:
        return struct.unpack('<f', f.read(4))[0]
    elif value_type == GGUF_TYPE_FLOAT64:
        return struct.unpack('<d', f.read(8))[0]
    elif value_type == GGUF_TYPE_BOOL:
        return struct.unpack('<?', f.read(1))[0]
    elif value_type == GGUF_TYPE_STRING:
        return _read_string(f)
    elif value_type == GGUF_TYPE_ARRAY:
        arr_type = struct.unpack('<I', f.read(4))[0]
        arr_len = struct.unpack('<Q', f.read(8))[0]
        return [_read_value(f, arr_type) for _ in range(arr_len)]
    else:
        raise ValueError(f"Unknown GGUF value type: {value_type}")


def _skip_array_elements(f, arr_type: int, count: int):
    """Skip array elements by type without allocating."""
    if arr_type in (GGUF_TYPE_UINT8, GGUF_TYPE_INT8, GGUF_TYPE_BOOL):
        f.read(count)
    elif arr_type in (GGUF_TYPE_UINT16, GGUF_TYPE_INT16):
        f.read(count * 2)
    elif arr_type in (GGUF_TYPE_UINT32, GGUF_TYPE_INT32, GGUF_TYPE_FLOAT32):
        f.read(count * 4)
    elif arr_type in (GGUF_TYPE_UINT64, GGUF_TYPE_INT64, GGUF_TYPE_FLOAT64):
        f.read(count * 8)
    elif arr_type == GGUF_TYPE_STRING:
        for _ in range(count):
            length = struct.unpack('<Q', f.read(8))[0]
            f.read(length)
    elif arr_type == GGUF_TYPE_ARRAY:
        for _ in range(count):
            inner_type = struct.unpack('<I', f.read(4))[0]
            inner_len = struct.unpack('<Q', f.read(8))[0]
            _skip_array_elements(f, inner_type, inner_len)


def _skip_value(f, value_type: int):
    """Skip a single GGUF value without parsing or allocating."""
    if value_type in (GGUF_TYPE_UINT8, GGUF_TYPE_INT8, GGUF_TYPE_BOOL):
        f.read(1)
    elif value_type in (GGUF_TYPE_UINT16, GGUF_TYPE_INT16):
        f.read(2)
    elif value_type in (GGUF_TYPE_UINT32, GGUF_TYPE_INT32, GGUF_TYPE_FLOAT32):
        f.read(4)
    elif value_type in (GGUF_TYPE_UINT64, GGUF_TYPE_INT64, GGUF_TYPE_FLOAT64):
        f.read(8)
    elif value_type == GGUF_TYPE_STRING:
        length = struct.unpack('<Q', f.read(8))[0]
        f.read(length)
    elif value_type == GGUF_TYPE_ARRAY:
        arr_type = struct.unpack('<I', f.read(4))[0]
        arr_len = struct.unpack('<Q', f.read(8))[0]
        _skip_array_elements(f, arr_type, arr_len)


def read_gguf_metadata(filepath: str) -> Dict[str, Any]:
    """Read all metadata key-value pairs from a GGUF file header."""
    metadata: Dict[str, Any] = {}

    with open(filepath, 'rb') as f:
        magic = struct.unpack('<I', f.read(4))[0]
        if magic != GGUF_MAGIC:
            raise ValueError(f"Not a GGUF file: magic=0x{magic:08x}")

        version = struct.unpack('<I', f.read(4))[0]

        if version == 1:
            tensor_count = struct.unpack('<I', f.read(4))[0]
            kv_count = struct.unpack('<I', f.read(4))[0]
        elif version in (2, 3):
            tensor_count = struct.unpack('<Q', f.read(8))[0]
            kv_count = struct.unpack('<Q', f.read(8))[0]
        else:
            raise ValueError(f"Unsupported GGUF version: {version}")

        for _ in range(kv_count):
            key = _read_string(f)
            value_type = struct.unpack('<I', f.read(4))[0]
            value = _read_value(f, value_type)
            metadata[key] = value

    return metadata


def get_model_context_length(filepath: str) -> Optional[int]:
    """Get the model's trained context length, stopping early.

    Unlike read_gguf_metadata(), this only parses the keys we need
    and skips everything else — including the 250K tokenizer string
    arrays that would otherwise OOM or timeout.
    """
    try:
        with open(filepath, 'rb') as f:
            magic = struct.unpack('<I', f.read(4))[0]
            if magic != GGUF_MAGIC:
                return None

            version = struct.unpack('<I', f.read(4))[0]
            if version == 1:
                f.read(4)  # tensor_count
                kv_count = struct.unpack('<I', f.read(4))[0]
            elif version in (2, 3):
                f.read(8)  # tensor_count
                kv_count = struct.unpack('<Q', f.read(8))[0]
            else:
                return None

            arch = None
            ctx = None

            for _ in range(kv_count):
                key = _read_string(f)
                value_type = struct.unpack('<I', f.read(4))[0]

                if key == "general.context_length":
                    ctx = int(_read_value(f, value_type))
                elif key == "general.architecture":
                    arch = str(_read_value(f, value_type))
                elif arch and key == f"{arch}.context_length":
                    ctx = int(_read_value(f, value_type))
                else:
                    _skip_value(f, value_type)

                # Early exit once we have both
                if ctx is not None and arch is not None:
                    if ctx > 0:
                        logger.info(f"GGUF context_length: {ctx} (arch: {arch})")
                        return ctx

            # Got through all keys
            if ctx is not None and ctx > 0:
                logger.info(f"GGUF context_length: {ctx}")
                return ctx

            logger.debug(f"No context_length found in GGUF metadata")
            return None

    except Exception as e:
        logger.debug(f"Could not read context length from {filepath}: {e}")
        return None


def get_model_architecture_info(filepath: str) -> Dict[str, Any]:
    """Get architecture info needed for memory estimation.

    Uses targeted parsing to avoid loading massive tokenizer arrays
    into memory (250K+ strings in some models).
    """
    default = {"architecture": "unknown", "context_length": 0, "n_layers": 0, "n_embd": 0, "n_head": 0}
    try:
        with open(filepath, 'rb') as f:
            magic = struct.unpack('<I', f.read(4))[0]
            if magic != GGUF_MAGIC:
                return default

            version = struct.unpack('<I', f.read(4))[0]
            if version == 1:
                f.read(4)
                kv_count = struct.unpack('<I', f.read(4))[0]
            elif version in (2, 3):
                f.read(8)
                kv_count = struct.unpack('<Q', f.read(8))[0]
            else:
                return default

            arch = None
            ctx_length = 0
            n_layers = 0
            n_embd = 0
            n_head = 0

            # Keys we always want
            needed_keys: set = {"general.architecture", "general.context_length"}

            for _ in range(kv_count):
                key = _read_string(f)
                value_type = struct.unpack('<I', f.read(4))[0]

                if key not in needed_keys:
                    _skip_value(f, value_type)
                    continue

                value = _read_value(f, value_type)

                if key == "general.architecture":
                    arch = str(value)
                    prefix = get_arch_prefix(arch)
                    # Now that we know the prefix, add arch-specific keys
                    needed_keys.update([
                        f"{prefix}.context_length",
                        f"{prefix}.block_count",
                        f"{prefix}.layer_count",
                        f"{prefix}.embedding_length",
                        f"{prefix}.hidden_size",
                        f"{prefix}.attention.head_count",
                        f"{prefix}.num_attention_heads",
                    ])

                elif key == "general.context_length":
                    ctx_length = int(value)

                elif key.endswith(".context_length") and ctx_length == 0:
                    ctx_length = int(value)

                elif key.endswith(".block_count") and n_layers == 0:
                    n_layers = int(value)

                elif key.endswith(".layer_count") and n_layers == 0:
                    n_layers = int(value)

                elif key.endswith(".embedding_length") and n_embd == 0:
                    n_embd = int(value)

                elif key.endswith(".hidden_size") and n_embd == 0:
                    n_embd = int(value)

                elif key.endswith(".attention.head_count") and n_head == 0:
                    n_head = int(value)

                elif key.endswith(".num_attention_heads") and n_head == 0:
                    n_head = int(value)

            return {
                "architecture": arch or "unknown",
                "context_length": ctx_length,
                "n_layers": n_layers,
                "n_embd": n_embd,
                "n_head": n_head,
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

    # head_dim determines per-token KV size
    head_dim = n_embd // n_head if n_head and n_head > 0 else 128

    # GQA: if fewer KV heads than attention heads, size is reduced
    kv_dim = n_embd
    if n_kv_heads and n_head and n_kv_heads < n_head:
        kv_dim = n_kv_heads * head_dim

    # KV cache: 2 (K + V) * n_layers * n_ctx * kv_dim * bytes_per_element
    total_bytes = 2 * n_layers * n_ctx * kv_dim * bytes_per_elem

    return round(total_bytes / (1024 ** 3), 2)
