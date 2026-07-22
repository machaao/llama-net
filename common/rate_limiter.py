import time
import asyncio
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field
from common.utils import get_logger

logger = get_logger(__name__)


@dataclass
class TokenBucket:
    """Token bucket for rate limiting"""
    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = 0
    last_refill: float = field(default_factory=time.time)

    def __post_init__(self):
        self.tokens = self.capacity

    def consume(self, tokens: float = 1.0) -> Tuple[bool, float]:
        """Try to consume tokens. Returns (allowed, retry_after_seconds)."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True, 0.0
        else:
            deficit = tokens - self.tokens
            retry_after = deficit / self.refill_rate
            return False, retry_after


class RateLimiter:
    """
    Multi-tier rate limiter with per-key, per-IP, and global limits.
    Uses in-memory token buckets. All limits are configurable.
    """

    def __init__(
        self,
        key_rpm: int = 60,
        key_rph: int = 1000,
        key_concurrent: int = 5,
        ip_rpm: int = 30,
        ip_rph: int = 300,
        global_rpm: int = 500,
        global_concurrent: int = 50,
        burst_rps: int = 10,
        burst_window: float = 1.0,
        node_publish_rph: int = 30,
        sse_per_ip: int = 5,
        sse_global: int = 200,
    ):
        self._lock = asyncio.Lock()
        self._key_minute_buckets: Dict[str, TokenBucket] = {}
        self._key_hour_buckets: Dict[str, TokenBucket] = {}
        self._key_burst_buckets: Dict[str, TokenBucket] = {}
        self._ip_minute_buckets: Dict[str, TokenBucket] = {}
        self._ip_hour_buckets: Dict[str, TokenBucket] = {}
        self._node_publish_buckets: Dict[str, TokenBucket] = {}
        self._global_minute_bucket = TokenBucket(
            capacity=global_rpm, refill_rate=global_rpm / 60.0
        )
        self._key_concurrent: Dict[str, int] = {}
        self._global_concurrent: int = 0
        self._sse_per_ip: Dict[str, int] = {}
        self._sse_global: int = 0
        self.key_rpm = key_rpm
        self.key_rph = key_rph
        self.key_concurrent = key_concurrent
        self.ip_rpm = ip_rpm
        self.ip_rph = ip_rph
        self.global_rpm = global_rpm
        self.global_concurrent = global_concurrent
        self.burst_rps = burst_rps
        self.burst_window = burst_window
        self.node_publish_rph = node_publish_rph
        self.sse_per_ip = sse_per_ip
        self.sse_global = sse_global
        self._last_cleanup = time.time()
        self._cleanup_interval = 300

    def _get_or_create_bucket(
        self, store: Dict[str, TokenBucket], key: str,
        capacity: float, refill_rate: float
    ) -> TokenBucket:
        if key not in store:
            store[key] = TokenBucket(capacity=capacity, refill_rate=refill_rate)
        return store[key]

    def _get_client_ip(self, request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return real_ip
        if hasattr(request, "client") and request.client:
            return request.client.host
        return "unknown"

    def _get_auth_key(self, request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    async def check_rate_limit(
        self, request, endpoint_type: str = "api"
    ) -> Tuple[bool, Dict]:
        client_ip = self._get_client_ip(request)
        auth_key = self._get_auth_key(request)
        now = time.time()

        if now - self._last_cleanup > self._cleanup_interval:
            await self._cleanup_stale_buckets()
            self._last_cleanup = now

        details = {"ip": client_ip, "endpoint_type": endpoint_type}

        allowed, retry = self._global_minute_bucket.consume(1.0)
        if not allowed:
            details.update({
                "limit_type": "global_rate", "retry_after": round(retry, 1),
                "message": "Server is at capacity. Please try again shortly."
            })
            return False, details

        if self._global_concurrent >= self.global_concurrent:
            details.update({
                "limit_type": "global_concurrent", "retry_after": 2.0,
                "message": "Server is processing maximum concurrent requests."
            })
            return False, details

        ip_minute = self._get_or_create_bucket(
            self._ip_minute_buckets, f"ip:{client_ip}",
            capacity=self.ip_rpm, refill_rate=self.ip_rpm / 60.0
        )
        allowed, retry = ip_minute.consume(1.0)
        if not allowed:
            details.update({
                "limit_type": "ip_rate", "retry_after": round(retry, 1),
                "message": f"Rate limit exceeded for your IP. {self.ip_rpm} requests/minute allowed."
            })
            return False, details

        ip_hour = self._get_or_create_bucket(
            self._ip_hour_buckets, f"ip:{client_ip}:hour",
            capacity=self.ip_rph, refill_rate=self.ip_rph / 3600.0
        )
        allowed, retry = ip_hour.consume(1.0)
        if not allowed:
            details.update({
                "limit_type": "ip_hourly", "retry_after": round(retry, 1),
                "message": f"Hourly rate limit exceeded. {self.ip_rph} requests/hour allowed."
            })
            return False, details

        if auth_key:
            key_minute = self._get_or_create_bucket(
                self._key_minute_buckets, f"key:{auth_key}",
                capacity=self.key_rpm, refill_rate=self.key_rpm / 60.0
            )
            allowed, retry = key_minute.consume(1.0)
            if not allowed:
                details.update({
                    "limit_type": "key_rate", "retry_after": round(retry, 1),
                    "message": f"Rate limit exceeded. {self.key_rpm} requests/minute allowed."
                })
                return False, details

            key_hour = self._get_or_create_bucket(
                self._key_hour_buckets, f"key:{auth_key}:hour",
                capacity=self.key_rph, refill_rate=self.key_rph / 3600.0
            )
            allowed, retry = key_hour.consume(1.0)
            if not allowed:
                details.update({
                    "limit_type": "key_hourly", "retry_after": round(retry, 1),
                    "message": f"Hourly rate limit exceeded. {self.key_rph} requests/hour allowed."
                })
                return False, details

            key_burst = self._get_or_create_bucket(
                self._key_burst_buckets, f"key:{auth_key}:burst",
                capacity=self.burst_rps, refill_rate=self.burst_rps / self.burst_window
            )
            allowed, retry = key_burst.consume(1.0)
            if not allowed:
                details.update({
                    "limit_type": "burst", "retry_after": round(retry, 2),
                    "message": f"Burst limit exceeded. {self.burst_rps} requests/second allowed."
                })
                return False, details

            key_current = self._key_concurrent.get(auth_key, 0)
            if key_current >= self.key_concurrent:
                details.update({
                    "limit_type": "key_concurrent", "retry_after": 2.0,
                    "message": f"Maximum {self.key_concurrent} concurrent requests per API key."
                })
                return False, details

        return True, details

    async def acquire_concurrent(self, auth_key: Optional[str] = None):
        self._global_concurrent += 1
        if auth_key:
            self._key_concurrent[auth_key] = self._key_concurrent.get(auth_key, 0) + 1

    async def release_concurrent(self, auth_key: Optional[str] = None):
        self._global_concurrent = max(0, self._global_concurrent - 1)
        if auth_key:
            self._key_concurrent[auth_key] = max(
                0, self._key_concurrent.get(auth_key, 0) - 1
            )

    async def check_node_publish(self, request) -> Tuple[bool, Dict]:
        client_ip = self._get_client_ip(request)
        now = time.time()
        if now - self._last_cleanup > self._cleanup_interval:
            await self._cleanup_stale_buckets()
            self._last_cleanup = now

        bucket = self._get_or_create_bucket(
            self._node_publish_buckets, f"publish:{client_ip}",
            capacity=self.node_publish_rph,
            refill_rate=self.node_publish_rph / 3600.0
        )
        allowed, retry = bucket.consume(1.0)
        if not allowed:
            return False, {
                "limit_type": "node_publish", "retry_after": round(retry, 1),
                "message": f"Node registration rate limit exceeded. {self.node_publish_rph}/hour allowed."
            }
        return True, {}

    async def check_sse_connection(self, request) -> Tuple[bool, Dict]:
        client_ip = self._get_client_ip(request)
        if self._sse_global >= self.sse_global:
            return False, {
                "limit_type": "sse_global",
                "message": f"Maximum {self.sse_global} concurrent SSE connections."
            }
        ip_count = self._sse_per_ip.get(client_ip, 0)
        if ip_count >= self.sse_per_ip:
            return False, {
                "limit_type": "sse_per_ip",
                "message": f"Maximum {self.sse_per_ip} SSE connections per IP."
            }
        return True, {}

    async def track_sse_open(self, request):
        client_ip = self._get_client_ip(request)
        self._sse_global += 1
        self._sse_per_ip[client_ip] = self._sse_per_ip.get(client_ip, 0) + 1

    async def track_sse_close(self, request):
        client_ip = self._get_client_ip(request)
        self._sse_global = max(0, self._sse_global - 1)
        self._sse_per_ip[client_ip] = max(
            0, self._sse_per_ip.get(client_ip, 0) - 1
        )

    async def _cleanup_stale_buckets(self):
        now = time.time()
        stale_threshold = 3600
        for store in [
            self._key_minute_buckets, self._key_hour_buckets,
            self._key_burst_buckets, self._ip_minute_buckets,
            self._ip_hour_buckets, self._node_publish_buckets,
        ]:
            stale_keys = [k for k, v in store.items() if now - v.last_refill > stale_threshold]
            for k in stale_keys:
                del store[k]
        zero_keys = [k for k, v in self._key_concurrent.items() if v <= 0]
        for k in zero_keys:
            del self._key_concurrent[k]

    def get_status(self) -> Dict:
        return {
            "global_concurrent": self._global_concurrent,
            "global_max": self.global_concurrent,
            "tracked_keys": len(self._key_minute_buckets),
            "tracked_ips": len(self._ip_minute_buckets),
            "sse_connections": self._sse_global,
            "sse_max": self.sse_global,
        }
