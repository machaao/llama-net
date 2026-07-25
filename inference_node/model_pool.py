"""
Multi-model pool with capacity-based LRU eviction.
Manages multiple loaded LLM instances simultaneously.
Evicts the least recently used model when capacity is exceeded.
"""

import os
import gc
import time
import psutil
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from common.utils import get_logger
from common.gguf_utils import get_model_architecture_info, estimate_kv_cache_gb

logger = get_logger(__name__)


@dataclass
class ModelSlot:
    """Container for a single loaded model in the pool."""
    model_name: str
    model_path: str
    llm: object  # LlamaWrapper instance
    metrics: object  # MetricsManager instance
    last_accessed: float = field(default_factory=time.time)
    loaded_at: float = field(default_factory=time.time)
    size_bytes: int = 0
    access_count: int = 0


class ModelPool:
    """
    Manages multiple loaded models with LRU eviction.
    Pool capacity is auto-detected from available RAM or set explicitly
    via MAX_MODELS env var. When full, loading a new model evicts the
    least recently used one.
    """

    DEFAULT_MODEL_SIZE_GB = 5.0  # Fallback when GGUF metadata unavailable

    def __init__(self, config):
        self.config = config
        self.slots: Dict[str, ModelSlot] = {}
        self.active_model: Optional[str] = None
        self.max_models: int = getattr(config, 'max_models', 0) or 0
        self.memory_budget_gb: float = getattr(config, 'memory_budget_gb', 0.0) or 0.0

        if self.max_models <= 0:
            self.max_models = self._detect_max_models()
        if self.memory_budget_gb <= 0:
            self.memory_budget_gb = self._detect_memory_budget()

        logger.info(
            f"Model pool initialized: max_models={self.max_models}, "
            f"memory_budget={self.memory_budget_gb:.1f} GB"
        )

    def _detect_max_models(self) -> int:
        try:
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)
            usable_gb = available_gb * 0.70
            per_model_gb = self._estimate_per_model_memory()
            max_count = max(1, min(5, int(usable_gb / per_model_gb)))
            logger.info(
                f"Auto-detected capacity: {available_gb:.1f} GB available, "
                f"{usable_gb:.1f} GB usable, {per_model_gb:.1f} GB/model → {max_count} slots"
            )
            return max_count
        except Exception as e:
            logger.warning(f"Could not auto-detect capacity: {e}, defaulting to 1")
            return 1

    def _estimate_per_model_memory(self) -> float:
        """Estimate per-model memory including KV cache."""
        model_gb = self.DEFAULT_MODEL_SIZE_GB
        kv_gb = 0.0

        # Try to get actual model file size
        model_path = getattr(self.config, 'model_path', '')
        if model_path and os.path.exists(model_path):
            try:
                model_gb = os.path.getsize(model_path) / (1024 ** 3)
            except OSError:
                pass

        # Estimate KV cache from model metadata + config
        if model_path and os.path.exists(model_path):
            try:
                arch_info = get_model_architecture_info(model_path)
                cache_type = getattr(self.config, 'cache_type_k', 'f16')
                n_ctx = getattr(self.config, 'n_ctx', 4096)
                kv_gb = estimate_kv_cache_gb(
                    n_layers=arch_info.get("n_layers", 32),
                    n_embd=arch_info.get("n_embd", 4096),
                    n_ctx=n_ctx,
                    cache_type=cache_type,
                    n_head=arch_info.get("n_head", 32),
                )
            except Exception:
                pass

        total = model_gb + kv_gb + 0.5  # model + kv + overhead
        logger.debug(f"Per-model memory estimate: {model_gb:.1f} GB weights + {kv_gb:.1f} GB KV = {total:.1f} GB total")
        return max(total, self.DEFAULT_MODEL_SIZE_GB)

    def _detect_memory_budget(self) -> float:
        try:
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)
            return round(available_gb * 0.70, 1)
        except Exception:
            return 20.0

    def register(self, model_path: str, llm, model_name: str = "") -> ModelSlot:
        if not model_name:
            model_name = os.path.basename(model_path)

        size_bytes = 0
        try:
            if os.path.exists(model_path):
                size_bytes = os.path.getsize(model_path)
        except OSError:
            pass

        slot = ModelSlot(
            model_name=model_name,
            model_path=model_path,
            llm=llm,
            metrics=llm.metrics_manager,
            size_bytes=size_bytes,
        )

        self.slots[model_name] = slot
        self.active_model = model_name

        logger.info(
            f"Registered model in pool: {model_name} "
            f"({self._format_size(size_bytes)}) "
            f"[{len(self.slots)}/{self.max_models} slots]"
        )
        return slot

    def get(self, model_name: str) -> Optional[ModelSlot]:
        return self.slots.get(model_name)

    def get_for_inference(self, model_name: str) -> Optional[ModelSlot]:
        slot = self.slots.get(model_name)
        if slot:
            slot.last_accessed = time.time()
            slot.access_count += 1
        return slot

    def activate(self, model_name: str) -> bool:
        if model_name not in self.slots:
            return False
        self.active_model = model_name
        self.slots[model_name].last_accessed = time.time()
        self.slots[model_name].access_count += 1
        logger.info(f"Activated model: {model_name}")
        return True

    def get_active(self) -> Optional[ModelSlot]:
        if self.active_model and self.active_model in self.slots:
            return self.slots[self.active_model]
        return None

    def get_active_llm(self):
        slot = self.get_active()
        return slot.llm if slot else None

    def load_model(self, model_path: str, model_name: str = "") -> ModelSlot:
        from inference_node.llm_wrapper import LlamaWrapper

        if not model_name:
            model_name = os.path.basename(model_path)

        # Already in pool — just activate
        if model_name in self.slots:
            self.activate(model_name)
            logger.info(f"Model already in pool, activated: {model_name}")
            return self.slots[model_name]

        # Check if we need to evict
        if len(self.slots) >= self.max_models:
            evicted_name = self.get_eviction_target()
            if evicted_name:
                logger.info(
                    f"Pool full ({len(self.slots)}/{self.max_models}), "
                    f"evicting LRU model: {evicted_name}"
                )
                self.evict(evicted_name)

        # Load the new model
        logger.info(f"Loading model into pool: {model_path}")

        from inference_node.config import InferenceConfig
        model_config = InferenceConfig.__new__(InferenceConfig)
        model_config.model_path = model_path
        model_config.model_name = model_name
        model_config.host = self.config.host
        model_config.port = self.config.port
        model_config.node_id = self.config.node_id
        model_config.n_ctx = self.config.n_ctx
        model_config.n_batch = self.config.n_batch
        model_config.n_gpu_layers = self.config.n_gpu_layers
        model_config.verbose = self.config.verbose
        model_config.no_model_mode = False
        model_config.flash_attn = getattr(self.config, 'flash_attn', False)         # NEW
        model_config.cache_type_k = getattr(self.config, 'cache_type_k', 'f16')     # NEW
        model_config.cache_type_v = getattr(self.config, 'cache_type_v', 'f16')     # NEW

        llm = LlamaWrapper(model_config)
        slot = self.register(model_path, llm, model_name)
        self.active_model = model_name
        return slot

    def get_eviction_target(self) -> Optional[str]:
        if not self.slots:
            return None
        candidates = {
            name: slot for name, slot in self.slots.items()
            if name != self.active_model
        }
        if not candidates:
            return None
        return min(candidates, key=lambda n: candidates[n].last_accessed)

    def evict(self, model_name: str) -> bool:
        slot = self.slots.get(model_name)
        if not slot:
            return False

        try:
            slot.llm.unload_model()
        except Exception as e:
            logger.warning(f"Error unloading model {model_name}: {e}")

        del self.slots[model_name]

        if self.active_model == model_name:
            if self.slots:
                self.active_model = next(iter(self.slots))
                logger.info(f"Active model evicted, switched to: {self.active_model}")
            else:
                self.active_model = None
                logger.info("All models evicted, pool empty")

        gc.collect()

        logger.info(
            f"Evicted model: {model_name} "
            f"[{len(self.slots)}/{self.max_models} slots remaining]"
        )
        return True

    def evict_lru(self) -> Optional[str]:
        target = self.get_eviction_target()
        if target:
            self.evict(target)
        return target

    def get_memory_usage(self) -> float:
        total_bytes = sum(slot.size_bytes for slot in self.slots.values())
        return round(total_bytes / (1024 ** 3), 2)

    def can_load(self, model_path: str = "") -> bool:
        if len(self.slots) < self.max_models:
            return True
        return len(self.slots) > 1

    def get_history(self) -> List[Dict[str, Any]]:
        history = []
        for name, slot in self.slots.items():
            history.append({
                "model_path": slot.model_path,
                "model_name": slot.model_name,
                "last_used": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(slot.last_accessed)
                ),
                "size_bytes": slot.size_bytes,
                "access_count": slot.access_count,
                "is_active": name == self.active_model,
            })
        history.sort(key=lambda h: h["last_used"], reverse=True)
        return history

    def get_network_info(self) -> Dict[str, Any]:
        return {
            "models": list(self.slots.keys()),
            "active_model": self.active_model,
            "capacity": self.max_models,
            "used": len(self.slots),
            "memory_used_gb": self.get_memory_usage(),
            "memory_budget_gb": self.memory_budget_gb,
        }

    def status(self) -> Dict[str, Any]:
        slots_info = []
        now = time.time()

        for name, slot in self.slots.items():
            age_seconds = now - slot.last_accessed
            slots_info.append({
                "model_name": slot.model_name,
                "model_path": slot.model_path,
                "is_active": name == self.active_model,
                "last_accessed_ago": round(age_seconds, 1),
                "last_accessed": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(slot.last_accessed)
                ),
                "loaded_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(slot.loaded_at)
                ),
                "access_count": slot.access_count,
                "size_bytes": slot.size_bytes,
                "size_display": self._format_size(slot.size_bytes),
                "metrics": slot.metrics.get_performance_metrics(),
            })

        slots_info.sort(key=lambda s: (not s["is_active"], -s["last_accessed_ago"]))

        lru_target = self.get_eviction_target()

        return {
            "enabled": True,
            "max_models": self.max_models,
            "used_slots": len(self.slots),
            "available_slots": self.max_models - len(self.slots),
            "active_model": self.active_model,
            "memory_used_gb": self.get_memory_usage(),
            "memory_budget_gb": self.memory_budget_gb,
            "memory_percent": round(
                (self.get_memory_usage() / self.memory_budget_gb * 100)
                if self.memory_budget_gb > 0 else 0, 1
            ),
            "lru_candidate": lru_target,
            "slots": slots_info,
            "timestamp": time.time(),
        }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes == 0:
            return "Unknown"
        gb = size_bytes / (1024 ** 3)
        if gb >= 1:
            return f"{gb:.1f} GB"
        mb = size_bytes / (1024 ** 2)
        return f"{mb:.0f} MB"

    def __len__(self) -> int:
        return len(self.slots)

    def __contains__(self, model_name: str) -> bool:
        return model_name in self.slots

    def __repr__(self) -> str:
        return (
            f"ModelPool({len(self.slots)}/{self.max_models} models, "
            f"active={self.active_model})"
        )
