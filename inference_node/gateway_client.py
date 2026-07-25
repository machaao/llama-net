"""
Gateway HTTP client — replaces DHT, P2P, and IP-based discovery.

One HTTP client to rule them all:
  - Registers with gateway
  - Sends heartbeats
  - Fetches peer list
  - Selects nodes for routing
  - Sends departure notifications
"""

import asyncio
import hashlib
import json
import os
import time
from typing import Dict, Any, Optional, List
import aiohttp
from common.utils import get_logger, get_host_ip, get_tunnel_url_file

logger = get_logger(__name__)


class GatewayClient:
    """HTTP client for communicating with the LlamaNet gateway."""

    def __init__(
        self,
        gateway_url: str,
        node_id: str,
        model_name: str,
        port: int,
        metrics_callback=None,
        tunnel_url: str = "",
        public_ip: str = "",
        model_pool=None,
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.node_id = node_id
        self.node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        self.model_name = model_name
        self.port = port
        self.metrics_callback = metrics_callback
        self.tunnel_url = tunnel_url or self._detect_tunnel_url()
        self.public_ip = public_ip  # Only use explicitly provided IP; no auto-detection needed (tunnel-only)
        self.own_url = self.tunnel_url  # Tunnel-only — no IP:port fallback
        self.model_pool = model_pool

        self.running = False
        self.registered = False
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.peer_refresh_task: Optional[asyncio.Task] = None

        # Cached peer list from gateway
        self._peers: List[Dict[str, Any]] = []
        self._peers_updated_at: float = 0
        self._rr_index: Dict[str, int] = {}

        # Quality gate rejection tracking
        self._quality_rejected: bool = False
        self._rejection_reason: str = ""

        # Self-reported native probe metrics (set after model loads)
        self.probe_metrics: Dict[str, Any] = {}

    # ─── lifecycle ───────────────────────────────────────────────

    async def register(self) -> bool:
        """Register this node with the gateway. Requires a tunnel URL."""
        self.running = True
        if not self.own_url:
            logger.warning("⚠️ No tunnel URL detected — node will NOT register with gateway.")
            logger.warning("   Start with --tunnel flag: ./start-app.sh run <model> --tunnel")
            return False
        metrics = self._get_metrics()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.post(
                    f"{self.gateway_url}/api/nodes/publish",
                    json={
                        "node_id": self.node_id,
                        "url": self.own_url,
                        "tunnel_url": self.tunnel_url,
                        "model": self.model_name,
                        "models": [self.model_name] + (
                            [m for m in (self.model_pool.get_network_info().get("models", [])) if m != self.model_name]
                            if self.model_pool else []
                        ),
                        "ip": self.public_ip,
                        "port": self.port,
                        "metrics": metrics,
                        "probe_metrics": self.probe_metrics,
                        "platform": self._get_platform_string(),
                        "gpu": self._get_gpu_info(),
                    },
                ) as resp:
                    if resp.status == 200:
                        self.registered = True

                        # ── Store per-node bearer token from gateway ──
                        try:
                            resp_data = await resp.json()
                            node_token = resp_data.get("node_token", "")
                            if node_token:
                                from common.gateway_auth import set_node_token
                                set_node_token(node_token)
                                logger.info("🔑 Node bearer token received from gateway")
                        except Exception:
                            logger.debug("No bearer token in registration response")

                        logger.info(f"✅ Registered with gateway: {self.gateway_url}")
                        return True
                    else:
                        text = await resp.text()
                        logger.warning(f"Registration failed ({resp.status}): {text}")

                        # If quality gate rejected us, check if it's permanent or transient
                        if resp.status == 403:
                            try:
                                body = json.loads(text)
                                failed_check = body.get("failed_check", "")
                                reason = body.get("reason", "quality gate rejection")

                                if failed_check == "hardware":
                                    # Hardware rejection is permanent — stop retrying
                                    self._quality_rejected = True
                                    self._rejection_reason = reason
                                    logger.warning(
                                        f"🚫 Node permanently rejected by hardware gate: {reason}"
                                    )
                                    logger.warning(
                                        f"   This node will NOT retry registration. "
                                        f"Fix the hardware issue or disable the quality gate."
                                    )
                                else:
                                    # Probe failure is transient — log but keep retrying
                                    logger.warning(
                                        f"⚠️ Node rejected by probe gate (transient): {reason}"
                                    )
                                    logger.warning(
                                        f"   Will retry registration on next heartbeat."
                                    )
                            except Exception:
                                # Cannot parse response — treat as transient
                                logger.warning(
                                    f"⚠️ Quality gate rejection (unparseable) — will retry"
                                )
                        return False
        except Exception as e:
            logger.warning(f"Could not register with gateway: {e}")
            return False

    async def unregister(self):
        """Notify gateway that this node is leaving. Retries on failure."""
        self.running = False
        if not self.registered:
            return

        payload = {
            "node_id": self.node_id,
            "model": self.model_name,
            "reason": "graceful_shutdown",
        }

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as session:
                    async with session.post(
                        f"{self.gateway_url}/api/nodes/unpublish",
                        json=payload,
                    ) as resp:
                        if resp.status == 200:
                            logger.info("✅ Unregistered from gateway")
                            self.registered = False
                            return
                        else:
                            logger.warning(f"Unregister returned {resp.status} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Unregister error (attempt {attempt + 1}): {e}")

            if attempt < 2:
                await asyncio.sleep(0.5)

        logger.warning("⚠️ Failed to unregister from gateway after 3 attempts")
        self.registered = False

    # ─── background loops ───────────────────────────────────────

    async def heartbeat_loop(self):
        """Send heartbeats every 10 seconds. Auto-registers if tunnel URL appears late."""
        last_heartbeat_time = time.time()

        while self.running:
            try:
                # ── Wake-from-sleep detection ──
                now = time.time()
                elapsed_since_last = now - last_heartbeat_time

                # If more than 60s passed since last heartbeat, system likely slept
                if elapsed_since_last > 60 and last_heartbeat_time > 0:
                    logger.warning(
                        f"⏰ Detected {elapsed_since_last:.0f}s gap — "
                        f"system may have hibernated. Re-registering..."
                    )

                    # Force re-detect tunnel URL (it may have changed)
                    fresh_url = self._detect_tunnel_url()
                    if fresh_url:
                        self.own_url = fresh_url
                        self.tunnel_url = fresh_url
                        logger.info(f"🔄 Refreshed tunnel URL after wake: {fresh_url}")

                    # Force re-register with gateway
                    self.registered = False
                    registered = await self.register()
                    if registered:
                        await self.send_event("node_joined")
                        logger.info("✅ Re-registered with gateway after wake")
                    else:
                        logger.warning("⚠️ Failed to re-register after wake — will retry")

                    last_heartbeat_time = now
                    await asyncio.sleep(5)  # Brief pause before normal heartbeat
                    continue

                last_heartbeat_time = now

                # Auto-detect tunnel URL if we didn't have one at startup
                if not self.own_url:
                    detected = self._detect_tunnel_url()
                    if detected:
                        self.own_url = detected
                        self.tunnel_url = detected
                        logger.info(f"🔄 Tunnel URL detected in heartbeat: {detected}")

                # Try to register if we have a URL but aren't registered
                if self.own_url and not self.registered:
                    if self._quality_rejected:
                        logger.debug(
                            f"Skipping registration — rejected by quality gate: "
                            f"{self._rejection_reason}"
                        )
                    else:
                        await self.register()
                        if self.registered:
                            await self.send_event("node_joined")

                # Send heartbeat only if registered
                if self.registered:
                    # Re-check tunnel URL in case it changed (quick tunnels)
                    fresh_url = self._detect_tunnel_url()
                    if fresh_url and fresh_url != self.own_url:
                        logger.info(f"🔄 Tunnel URL changed: {self.own_url} → {fresh_url}")
                        self.own_url = fresh_url
                        self.tunnel_url = fresh_url
                        # Re-register with new URL
                        await self.send_event("node_updated")

                    metrics = self._get_metrics()
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as session:
                        async with session.post(
                            f"{self.gateway_url}/api/nodes/heartbeat",
                            json={
                                "node_hash": self.node_hash,
                                "url": self.own_url,
                                "metrics": metrics,
                                "models": self._get_models_with_context(),
                            },
                        ) as resp:
                            if resp.status != 200:
                                logger.debug(f"Heartbeat returned {resp.status}")
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

            await asyncio.sleep(30)  # Reduced from 10s to 30s — less gateway load, still 3× redundancy before stale

    async def peer_refresh_loop(self):
        """Refresh cached peer list every 30 seconds."""
        while self.running:
            try:
                await self._refresh_peers()
            except Exception as e:
                logger.debug(f"Peer refresh error: {e}")

            await asyncio.sleep(30)

    # ─── peer list / node selection ──────────────────────────────

    async def get_peers(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Get list of known peers (cached, refreshed every 30s)."""
        if force_refresh or not self._peers:
            await self._refresh_peers()
        return list(self._peers)

    async def select_node(
        self, model: str = None, strategy: str = "load_balanced"
    ) -> Optional[Dict[str, Any]]:
        """Select best peer node for a request."""
        peers = await self.get_peers()

        # Filter by model if specified
        if model:
            model_lower = model.lower()
            filtered = [p for p in peers if model_lower in p.get("model", "").lower()]
            if filtered:
                peers = filtered

        # Exclude ourselves
        peers = [p for p in peers if p.get("node_id") != self.node_id]

        if not peers:
            return None

        if strategy == "round_robin":
            idx = self._rr_index.get(model, 0) % len(peers)
            self._rr_index[model] = idx + 1
            return peers[idx]
        elif strategy == "random":
            import random
            return random.choice(peers)
        else:
            # load_balanced — pick lowest load
            return min(peers, key=lambda p: p.get("load", 1.0))

    async def send_event(self, event_type: str, extra_data: Dict[str, Any] = None):
        """Send an event to the gateway (node_joined, node_left, node_updated)."""
        try:
            metrics = self._get_metrics()

            # Ensure pool_models is present as a top-level list for the gateway
            # GatewayClient._get_metrics() stores pool info under metrics['pool']
            # but the gateway endpoint reads from body.get("models", []) and
            # body.get("metrics", {}).get("pool_models", [])
            if self.model_pool:
                pool_info = self.model_pool.get_network_info()
                models_list = pool_info.get("models", [])
                if models_list:
                    # Object format: [{"name": "model", "ctx_length": N}]
                    metrics["pool_models"] = models_list
                    metrics["pool_size"] = len(models_list)

            payload = {
                "event_type": event_type,
                "node_id": self.node_id,
                "model": self.model_name,
                "url": self.own_url,
                "ip": self.public_ip,
                "port": self.port,
                "models": self._get_models_with_context(),
                "ctx_length": self._get_active_context_length_value(),
                "metrics": metrics,
                "probe_metrics": self.probe_metrics,
                "platform": self._get_platform_string(),
                "gpu": self._get_gpu_info(),
                **(extra_data or {}),
            }

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.post(
                    f"{self.gateway_url}/api/nodes/event",
                    json=payload,
                ) as resp:
                    logger.debug(f"Event '{event_type}' sent → {resp.status}")
        except Exception as e:
            logger.debug(f"Event '{event_type}' failed: {e}")

    # ─── internal helpers ────────────────────────────────────────

    async def _refresh_peers(self):
        """Fetch current peers from gateway."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(
                    f"{self.gateway_url}/api/models"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = data.get("models", [])

                        peers = []
                        for model_info in models:
                            for node in model_info.get("nodes", []):
                                node_url = node.get("url", "")
                                if not node_url:
                                    continue  # Skip nodes without a tunnel URL
                                peers.append({
                                    "node_id": node.get("node_hash", ""),
                                    "model": model_info.get("model_name", ""),
                                    "url": node_url,
                                    "load": node.get("load", 0),
                                    "tps": node.get("tps", 0),
                                    "gpu_info": node.get("gpu_info", ""),
                                    "ttft": node.get("ttft"),
                                    "latency": node.get("latency"),
                                    "total_tokens": node.get("total_tokens", 0),
                                    "uptime": node.get("uptime", 0),
                                    "last_seen": node.get("last_seen"),
                                    "ip": node.get("ip", ""),
                                    "port": node.get("port", 0),
                                })

                        self._peers = peers
                        self._peers_updated_at = time.time()
                        logger.debug(f"Refreshed {len(peers)} peers from gateway")
        except Exception as e:
            logger.debug(f"Peer refresh failed: {e}")

    def _get_metrics(self) -> Dict[str, Any]:
        """Get current metrics from callback, including pool info."""
        metrics = {}
        if self.metrics_callback:
            try:
                metrics = self.metrics_callback()
            except Exception:
                pass
        else:
            metrics = {"load": 0, "tps": 0, "uptime": 0}

        # Include pool info if available
        if self.model_pool:
            metrics['pool'] = self.model_pool.get_network_info()

        return metrics

    @staticmethod
    def _get_platform_string() -> str:
        """Get OS-architecture string for quality gate hardware detection."""
        import platform as p
        return f"{p.system()}-{p.machine()}"

    def _get_models_with_context(self) -> list:
        """Build models list with ctx_length for each model in pool."""
        models = [{"name": self.model_name, "ctx_length": 0}]
        if self.model_pool:
            try:
                pool_info = self.model_pool.get_network_info()
                models = pool_info.get("models", [])
                active_name = self.model_name
                names = {m["name"] for m in models}
                if active_name not in names:
                    active_slot = self.model_pool.get_active()
                    ctx = active_slot.n_ctx if active_slot else 0
                    models.insert(0, {"name": active_name, "ctx_length": ctx})
            except Exception:
                pass
        return models

    def _get_active_context_length_value(self) -> int:
        """Get ctx_length for the active model."""
        if self.model_pool:
            slot = self.model_pool.get_active()
            if slot:
                return slot.n_ctx
        return 0

    @staticmethod
    def _get_gpu_info() -> str:
        """Get GPU description string via SystemInfo."""
        try:
            from inference_node.metrics import SystemInfo
            return SystemInfo.get_gpu_info() or ""
        except Exception:
            return ""

    @staticmethod
    def _detect_tunnel_url() -> str:
        """Detect tunnel URL from env or file."""
        url = os.environ.get("LLAMANET_TUNNEL_URL", "")
        if url:
            return url.rstrip("/")

        tunnel_file = get_tunnel_url_file()
        try:
            if os.path.exists(tunnel_file):
                with open(tunnel_file) as f:
                    url = f.read().strip()
                    if url.startswith("http"):
                        return url.rstrip("/")
        except Exception:
            pass
        return ""
