import asyncio
import json
import uuid
import time
import aiohttp
from typing import Dict, Any, Tuple, Optional
from common.utils import get_logger

logger = get_logger(__name__)


class HTTPDHTTransport:
    """HTTP transport for DHT messages — enables DHT through Cloudflare tunnels"""

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        # Map "ip:dht_port" -> http_url for contacts discovered via HTTP
        self.peer_urls: Dict[str, str] = {}

    def register_peer_url(self, ip: str, port: int, http_url: str):
        """Register the HTTP URL for a DHT contact"""
        key = f"{ip}:{port}"
        self.peer_urls[key] = http_url.rstrip("/")
        logger.debug(f"Registered HTTP peer URL: {key} -> {http_url}")

    def get_peer_url(self, ip: str, port: int) -> Optional[str]:
        """Get the HTTP URL for a DHT contact"""
        return self.peer_urls.get(f"{ip}:{port}")

    async def send_request(
        self, message: Dict[str, Any], addr: Tuple[str, int], timeout: float = None
    ) -> Optional[Dict[str, Any]]:
        """Send a DHT message via HTTP POST and wait for response"""
        ip, port = addr
        url = self.get_peer_url(ip, port)

        if not url:
            logger.debug(f"No HTTP URL registered for {ip}:{port}")
            return None

        endpoint = f"{url}/dht/rpc"

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout or self.timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(
                    endpoint,
                    json=message,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.debug(
                            f"HTTP DHT request failed: {response.status} from {endpoint}"
                        )
                        return None
        except asyncio.TimeoutError:
            logger.debug(f"HTTP DHT request timeout to {endpoint}")
            return None
        except Exception as e:
            logger.debug(f"HTTP DHT request error to {endpoint}: {e}")
            return None
