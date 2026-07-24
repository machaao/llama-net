import os
import time
import asyncio
import aiohttp
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from common.utils import get_logger

logger = get_logger(__name__)


@dataclass
class ProbeResult:
    """Result of an inference probe against a node."""
    ttft: float = 0.0
    latency: float = 0.0
    tps: float = 0.0
    completion_tokens: int = 0
    success: bool = False
    error: Optional[str] = None


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
        self.probe_timeout = int(os.environ.get("LLAMANET_PROBE_TIMEOUT", "30"))

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
            f"min_tps={self.min_tps}, probe_timeout={self.probe_timeout}"
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

    # ── Inference Probe (async, network call) ────────────────────

    async def probe_node(
        self, tunnel_url: str, model_name: str = ""
    ) -> ProbeResult:
        """Send a minimal inference request to the node's tunnel URL.

        Measures TTFT, latency, and TPS from the actual response.
        Returns ProbeResult with success=True/False.
        """
        if not self.enabled:
            return ProbeResult(success=True)

        if not tunnel_url or not tunnel_url.startswith("http"):
            return ProbeResult(success=False, error="no tunnel URL")

        probe_url = f"{tunnel_url.rstrip('/')}/v1/chat/completions"

        body = {
            "model": model_name or "probe",
            "messages": [{"role": "user", "content": "Say hi"}],
            "max_tokens": 10,
            "stream": False,
        }

        start_time = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=self.probe_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    probe_url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    latency = time.time() - start_time

                    if resp.status != 200:
                        error_text = await resp.text()
                        return ProbeResult(
                            success=False,
                            latency=latency,
                            error=f"HTTP {resp.status}: {error_text[:200]}",
                        )

                    data = await resp.json()
                    completion_tokens = (
                        data.get("usage", {}).get("completion_tokens", 0)
                    )
                    # For non-streaming, TTFT ≈ total latency
                    ttft = latency
                    tps = completion_tokens / latency if latency > 0 else 0

                    logger.info(
                        f"Probe OK: ttft={ttft:.2f}s, latency={latency:.2f}s, "
                        f"tps={tps:.1f}, tokens={completion_tokens}"
                    )

                    return ProbeResult(
                        ttft=ttft,
                        latency=latency,
                        tps=tps,
                        completion_tokens=completion_tokens,
                        success=True,
                    )

        except asyncio.TimeoutError:
            latency = time.time() - start_time
            return ProbeResult(
                success=False,
                latency=latency,
                error=f"probe timeout after {self.probe_timeout}s",
            )
        except aiohttp.ClientConnectionError as e:
            latency = time.time() - start_time
            return ProbeResult(
                success=False,
                latency=latency,
                error=f"connection error: {str(e)[:200]}",
            )
        except Exception as e:
            latency = time.time() - start_time
            return ProbeResult(
                success=False,
                latency=latency,
                error=f"probe error: {str(e)[:200]}",
            )

    # ── Probe Evaluation ─────────────────────────────────────────

    def evaluate_probe(self, probe: ProbeResult) -> Tuple[bool, str]:
        """Check probe results against performance thresholds.

        Returns (passed, reason). When the gate is disabled, always
        returns (True, "gate disabled").
        """
        if not self.enabled:
            return True, "gate disabled"

        if not probe.success:
            return False, f"probe failed: {probe.error}"

        failed: List[str] = []

        # ── Max TTFT ──
        if self.max_ttft > 0 and probe.ttft > self.max_ttft:
            failed.append(
                f"ttft {probe.ttft:.2f}s > max {self.max_ttft}s"
            )

        # ── Max Latency ──
        if self.max_latency > 0 and probe.latency > self.max_latency:
            failed.append(
                f"latency {probe.latency:.2f}s > max {self.max_latency}s"
            )

        # ── Min TPS (only if node produced tokens) ──
        if self.min_tps > 0 and probe.completion_tokens > 0:
            if probe.tps < self.min_tps:
                failed.append(
                    f"tps {probe.tps:.2f} < min {self.min_tps}"
                )

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
            "probe_timeout": self.probe_timeout,
        }
