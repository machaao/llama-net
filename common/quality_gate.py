import os
from typing import List, Dict, Any, Tuple
from common.utils import get_logger

logger = get_logger(__name__)


class NodeQualityGate:
    """Configurable quality gate for node admission to the public network.

    Reads thresholds from environment variables. All thresholds default to
    disabled (0/false/"") so existing deployments are completely unaffected.

    Used on the gateway side to validate nodes at registration time.
    """

    def __init__(self):
        self.require_gpu = os.environ.get(
            "LLAMANET_REQUIRE_GPU", "false"
        ).lower() in ("true", "1", "yes")

        self.require_tunnel = os.environ.get(
            "LLAMANET_REQUIRE_TUNNEL", "false"
        ).lower() in ("true", "1", "yes")

        self.max_ttft = float(os.environ.get("LLAMANET_MAX_TTFT", "0"))
        self.max_latency = float(os.environ.get("LLAMANET_MAX_LATENCY", "0"))
        self.min_tps = float(os.environ.get("LLAMANET_MIN_TPS", "0"))
        exclude_raw = os.environ.get("LLAMANET_EXCLUDE_HARDWARE", "")
        self.excluded_hardware: List[str] = [
            p.strip().lower() for p in exclude_raw.split(",") if p.strip()
        ]

        self.enabled = any([
            self.require_gpu,
            self.require_tunnel,
            self.excluded_hardware,
            self.max_ttft > 0,
            self.max_latency > 0,
            self.min_tps > 0,
        ])

        logger.info(
            f"Quality gate initialized: enabled={self.enabled}, "
            f"require_gpu={self.require_gpu}, require_tunnel={self.require_tunnel}, "
            f"excluded_hardware={self.excluded_hardware}, "
            f"max_ttft={self.max_ttft}, max_latency={self.max_latency}, "
            f"min_tps={self.min_tps}"
        )

    # ── Hardware Gate (instant, no network) ──────────────────────

    def evaluate_hardware(
        self,
        platform: str = "",
        gpu_info: str = "",
        tunnel_url: str = "",
        url: str = "",
    ) -> Tuple[bool, str]:
        """Instant hardware/platform check. No network call.

        Returns (passed, reason). When the gate is disabled, always
        returns (True, "gate disabled").
        """
        if not self.enabled:
            return True, "gate disabled"

        # ── Tunnel URL required ──
        if self.require_tunnel:
            effective_url = tunnel_url or url
            if not effective_url or not effective_url.startswith("http"):
                return False, "tunnel_url required but not provided"

        # ── Hardware exclusion ──
        if self.excluded_hardware:
            hw_id = self._build_hardware_identifier(gpu_info, platform)
            for pattern in self.excluded_hardware:
                if pattern in hw_id:
                    return False, f"excluded hardware: '{hw_id}' matches '{pattern}'"

        # ── GPU required ──
        if self.require_gpu:
            if not self._detect_gpu_presence(gpu_info):
                return False, f"GPU required but none detected (gpu_info='{gpu_info}')"

        return True, "hardware check passed"

    # ── Performance Evaluation (self-reported native probe metrics) ─

    def evaluate_metrics(self, metrics: Dict[str, Any]) -> Tuple[bool, str]:
        """Check self-reported performance metrics against thresholds.

        The inference node measures its own TTFT/latency/TPS via a native
        local inference and reports them in the registration payload.

        Returns (passed, reason). When the gate is disabled, always
        returns (True, "gate disabled").
        """
        if not self.enabled:
            return True, "gate disabled"

        probe_success = metrics.get("probe_success", False)
        if not probe_success:
            return False, f"probe failed: {metrics.get('error', 'native probe did not succeed')}"

        failed: List[str] = []

        ttft = metrics.get("ttft", 0)
        latency = metrics.get("latency", 0)
        tps = metrics.get("tps", 0)
        tokens = metrics.get("completion_tokens", 0)

        if self.max_ttft > 0 and ttft > self.max_ttft:
            failed.append(f"ttft {ttft:.2f}s > max {self.max_ttft}s")

        if self.max_latency > 0 and latency > self.max_latency:
            failed.append(f"latency {latency:.2f}s > max {self.max_latency}s")

        if self.min_tps > 0 and tokens > 0:
            if tps < self.min_tps:
                failed.append(f"tps {tps:.2f} < min {self.min_tps}")

        if failed:
            return False, f"performance check failed: {'; '.join(failed)}"

        return True, "probe check passed"

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _build_hardware_identifier(gpu_info: str, platform: str) -> str:
        """Build a lowercase identifier string for pattern matching."""
        parts: List[str] = []
        gpu_lower = (gpu_info or "").lower()
        platform_lower = (platform or "").lower()

        # Detect Intel Mac
        if "intel" in gpu_lower or "intel" in platform_lower:
            parts.append("intel-mac")
        if "darwin" in platform_lower and "x86_64" in platform_lower:
            parts.append("intel-mac")
        if "darwin" in platform_lower and "x86" in platform_lower:
            parts.append("intel-mac")

        # Detect CPU-only (no GPU info)
        if not gpu_info or gpu_lower in ("none", "cpu", "cpu only", ""):
            parts.append("cpu-only")
            parts.append("x86_64-no-gpu")

        # Detect Apple Silicon
        if any(k in gpu_lower for k in ("apple", "metal", "m1", "m2", "m3", "m4")):
            parts.append("apple-silicon")
        if "darwin" in platform_lower and "arm64" in platform_lower:
            parts.append("apple-silicon")

        # Detect NVIDIA
        if any(k in gpu_lower for k in ("nvidia", "cuda", "geforce", "rtx", "gtx", "a100", "v100")):
            parts.append("nvidia")

        return ",".join(sorted(set(parts))) if parts else "unknown"

    @staticmethod
    def _detect_gpu_presence(gpu_info: str) -> bool:
        """Detect if GPU acceleration is available."""
        if not gpu_info:
            return False
        gpu_lower = gpu_info.lower().strip()
        if gpu_lower in ("none", "cpu", "cpu only", ""):
            return False
        return True

    def get_config_summary(self) -> Dict[str, Any]:
        """Return current configuration for logging/debugging."""
        return {
            "enabled": self.enabled,
            "require_gpu": self.require_gpu,
            "require_tunnel": self.require_tunnel,
            "excluded_hardware": self.excluded_hardware,
            "max_ttft": self.max_ttft,
            "max_latency": self.max_latency,
            "min_tps": self.min_tps,
        }
