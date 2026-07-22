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
import os
import time
from typing import Dict, Any, Optional, List
import aiohttp
from common.utils import get_logger, get_host_ip

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
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.node_id = node_id
        self.node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        self.model_name = model_name
        self.port = port
        self.metrics_callback = metrics_callback
        self.tunnel_url = tunnel_url or self._detect_tunnel_url()
        self.public_ip = public_ip or get_host_ip()
        self.own_url = self.tunnel_url  # Tunnel-only — no IP:port fallback

        self.running = False
        self.registered = False
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.peer_refresh_task: Optional[asyncio.Task] = None

        # Cached peer list from gateway
        self._peers: List[Dict[str, Any]] = []
        self._peers_updated_at: float = 0
        self._rr_index: Dict[str, int] = {}

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
                        "ip": self.public_ip,
                        "port": self.port,
                        "metrics": metrics,
                    },
                ) as resp:
                    if resp.status == 200:
                        self.registered = True
                        logger.info(f"✅ Registered with gateway: {self.gateway_url}")
                        return True
                    else:
                        text = await resp.text()
                        logger.warning(f"Registration failed ({resp.status}): {text}")
                        return False
        except Exception as e:
            logger.warning(f"Could not register with gateway: {e}")
            return False

    async def unregister(self):
        """Notify gateway that this node is leaving."""
        self.running = False
        if not self.registered:
            return

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.post(
                    f"{self.gateway_url}/api/nodes/unpublish",
                    json={
                        "node_id": self.node_id,
                        "model": self.model_name,
                        "reason": "graceful_shutdown",
                    },
                ) as resp:
                    if resp.status == 200:
                        logger.info("✅ Unregistered from gateway")
                    else:
                        logger.warning(f"Unregister returned {resp.status}")
        except Exception as e:
            logger.debug(f"Unregister error: {e}")
        finally:
            self.registered = False

    # ─── background loops ───────────────────────────────────────

    async def heartbeat_loop(self):
        """Send heartbeats every 10 seconds."""
        while self.running:
            try:
                metrics = self._get_metrics()

                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as session:
                    async with session.post(
                        f"{self.gateway_url}/api/nodes/heartbeat",
                        json={
                            "node_hash": self.node_hash,
                            "metrics": metrics,
                        },
                    ) as resp:
                        if resp.status != 200:
                            logger.debug(f"Heartbeat returned {resp.status}")
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

            await asyncio.sleep(10)

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
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.post(
                    f"{self.gateway_url}/api/nodes/event",
                    json={
                        "event_type": event_type,
                        "node_id": self.node_id,
                        "model": self.model_name,
                        "url": self.own_url,
                        "ip": self.public_ip,
                        "port": self.port,
                        "metrics": self._get_metrics(),
                        **(extra_data or {}),
                    },
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
                                })

                        self._peers = peers
                        self._peers_updated_at = time.time()
                        logger.debug(f"Refreshed {len(peers)} peers from gateway")
        except Exception as e:
            logger.debug(f"Peer refresh failed: {e}")

    def _get_metrics(self) -> Dict[str, Any]:
        """Get current metrics from callback."""
        if self.metrics_callback:
            try:
                return self.metrics_callback()
            except Exception:
                pass
        return {"load": 0, "tps": 0, "uptime": 0}

    @staticmethod
    def _detect_tunnel_url() -> str:
        """Detect tunnel URL from env or file."""
        url = os.environ.get("LLAMANET_TUNNEL_URL", "")
        if url:
            return url.rstrip("/")

        tunnel_file = "/tmp/llamanet_tunnel_url"
        try:
            if os.path.exists(tunnel_file):
                with open(tunnel_file) as f:
                    url = f.read().strip()
                    if url.startswith("http"):
                        return url.rstrip("/")
        except Exception:
            pass
        return ""
