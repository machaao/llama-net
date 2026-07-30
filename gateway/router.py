import asyncio
import hashlib
import json
import os
import time
import aiohttp
from typing import Dict, Any, Optional, List
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse
from common.utils import get_logger

logger = get_logger(__name__)

# Model size tiers for cost estimation (parameter count → multiplier)
_MODEL_SIZE_TIERS = [
    ("35b", 8), ("32b", 8), ("30b", 8), ("27b", 8), ("24b", 8), ("20b", 6),
    ("14b", 4), ("13b", 4), ("12b", 4), ("11b", 4),
    ("8b", 2), ("7b", 2), ("6b", 2),
    ("3b", 1), ("2b", 1), ("1b", 1), ("0.5b", 1),
]


def _estimate_cost_multiplier(model_name: str) -> int:
    """Estimate relative compute cost from model name."""
    name_lower = model_name.lower()
    for pattern, multiplier in _MODEL_SIZE_TIERS:
        if pattern in name_lower:
            return multiplier
    return 2


def _estimate_request_cost(max_tokens: int, model_name: str) -> float:
    """Estimate compute units for a request."""
    multiplier = _estimate_cost_multiplier(model_name)
    return (max_tokens / 100.0) * multiplier


class ModelRouter:
    def __init__(self, supabase_manager, node_token_manager=None):
        self.db = supabase_manager
        self._node_token_manager = node_token_manager
        self._rr_index = {}
        # Prefix affinity for KV cache reuse (sticky routing)
        self._prefix_affinity: Dict[str, str] = {}  # prefix_hash -> node_hash
        self._affinity_ttl: Dict[str, float] = {}   # prefix_hash -> last_used timestamp
        self._affinity_max_age = 1800  # 30 minutes
        # Per-key in-flight request tracking for fairness
        self._key_inflight: Dict[str, int] = {}
        self._max_per_key_concurrent = int(
            os.environ.get("LLAMANET_MAX_KEY_CONCURRENT", "3")
        )
        self._hourly_compute_budget = float(
            os.environ.get("LLAMANET_HOURLY_COMPUTE_BUDGET", "10000")
        )
        # Per-key hourly compute tracking
        self._key_hourly_compute: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _estimate_input_tokens(body: dict) -> int:
        """Estimate input token count from request body for context-aware routing.

        Uses a simple heuristic: ~4 characters per token.
        Only used for routing decisions — not precise enough for billing.
        """
        messages = body.get("messages", [])
        if not messages:
            prompt = body.get("prompt", "")
            if isinstance(prompt, list):
                prompt = " ".join(prompt)
            return max(1, len(prompt) // 4)

        total_chars = 0
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)

        return max(1, total_chars // 4)

    # ── Budget & Fairness Checks ─────────────────────────────────

    async def check_token_budget(self, auth_key: str) -> Optional[JSONResponse]:
        """Check if the API key has exceeded its daily token budget."""
        if not auth_key:
            return None
        key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
        usage = self.db.get_token_usage(key_hash)
        budget = self.db.get_daily_token_budget()
        consumed = usage.get("tokens_consumed", 0)
        if consumed >= budget:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": f"Daily token budget exceeded ({consumed:,}/{budget:,} tokens used). Resets at midnight UTC.",
                        "type": "token_budget_exceeded",
                        "tokens_used": consumed,
                        "tokens_budget": budget,
                    }
                },
                headers={"X-Token-Remaining": "0", "X-Token-Budget": str(budget)},
            )
        return None

    async def check_compute_budget(self, auth_key: str, estimated_cost: float) -> Optional[JSONResponse]:
        """Check if the API key has exceeded its hourly compute budget."""
        if not auth_key:
            return None
        key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
        current_hour = int(time.time()) // 3600

        async with self._lock:
            entry = self._key_hourly_compute.get(key_hash, {"hour": 0, "used": 0.0})
            if entry["hour"] != current_hour:
                entry = {"hour": current_hour, "used": 0.0}
            if entry["used"] + estimated_cost > self._hourly_compute_budget:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "message": "Hourly compute budget exceeded. Try smaller requests or wait.",
                            "type": "compute_budget_exceeded",
                            "compute_used": round(entry["used"], 1),
                            "compute_budget": self._hourly_compute_budget,
                        }
                    },
                )
        return None

    async def _acquire_key_slot(self, auth_key: str) -> Optional[JSONResponse]:
        """Enforce per-key concurrency limit."""
        if not auth_key:
            return None
        key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
        async with self._lock:
            current = self._key_inflight.get(key_hash, 0)
            if current >= self._max_per_key_concurrent:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "message": f"Maximum {self._max_per_key_concurrent} concurrent requests per API key.",
                            "type": "concurrent_limit",
                        }
                    },
                )
            self._key_inflight[key_hash] = current + 1
        return None

    async def _release_key_slot(self, auth_key: str):
        """Release a concurrency slot after request completes."""
        if not auth_key:
            return
        key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
        async with self._lock:
            current = self._key_inflight.get(key_hash, 0)
            self._key_inflight[key_hash] = max(0, current - 1)

    async def _record_compute_usage(self, auth_key: str, cost: float):
        """Record compute usage for an API key."""
        if not auth_key:
            return
        key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
        current_hour = int(time.time()) // 3600
        async with self._lock:
            entry = self._key_hourly_compute.get(key_hash, {"hour": 0, "used": 0.0})
            if entry["hour"] != current_hour:
                entry = {"hour": current_hour, "used": 0.0}
            entry["used"] += cost
            self._key_hourly_compute[key_hash] = entry

    def _compute_prefix_hash(self, body: Dict) -> Optional[str]:
        """Compute a stable hash from system prompt + conversation prefix for sticky routing."""
        messages = body.get("messages", [])
        if not messages:
            return None

        # Hash system message + first user message for stability
        parts = []
        for msg in messages[:3]:
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            if role in ("system", "user") and content:
                parts.append(f"{role}:{content}")

        if not parts:
            return None

        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    def _cleanup_affinity(self):
        """Remove expired prefix affinity mappings."""
        now = time.time()
        expired = [k for k, v in self._affinity_ttl.items() if now - v > self._affinity_max_age]
        for k in expired:
            self._prefix_affinity.pop(k, None)
            self._affinity_ttl.pop(k, None)
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired prefix affinity mappings")

    def _get_auth_key(self, request: Request) -> Optional[str]:
        """Extract the API key from the request."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    # ── Routing ──────────────────────────────────────────────────

    async def route_chat_completion(self, request: Request, user_id: str):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})

        model_name = body.get("model", "")
        strategy = body.pop("strategy", "load_balanced")
        stream = body.get("stream", False)
        max_tokens = body.get("max_tokens", 100)
        auth_key = self._get_auth_key(request)

        concurrency_err = await self._acquire_key_slot(auth_key)
        if concurrency_err:
            return concurrency_err

        try:
            budget_err = await self.check_token_budget(auth_key)
            if budget_err:
                return budget_err

            estimated_cost = _estimate_request_cost(max_tokens, model_name)
            compute_err = await self.check_compute_budget(auth_key, estimated_cost)
            if compute_err:
                return compute_err

            # ── Prefix-aware sticky routing for KV cache reuse ──
            conversation_id = body.get("conversation_id")
            prefix_hash = None
            if conversation_id:
                prefix_hash = self._compute_prefix_hash(body)

            # ── Estimate required context length ──
            input_tokens = self._estimate_input_tokens(body)
            min_ctx = input_tokens + max_tokens + 256

            node = await self._select_node(model_name, strategy, prefix_hash=prefix_hash, min_ctx_length=min_ctx)
            if not node:
                return JSONResponse(status_code=503, content={
                    "error": {"message": f"No nodes available for model '{model_name}'", "type": "server_error", "code": "no_nodes_available"}
                })

            routing_log = f"Routing chat completion for '{model_name}' to {node['node_hash'][:8]} (ctx={min_ctx})..."
            if prefix_hash:
                routing_log += f" (prefix={prefix_hash[:8]}...)"
            logger.info(routing_log)
            url = f"{node['url'].rstrip('/')}/v1/chat/completions"
            response = await self._forward_request(url, body, stream, node, auth_key, estimated_cost, model_name=model_name)
            await self._record_compute_usage(auth_key, estimated_cost)
            return response
        finally:
            await self._release_key_slot(auth_key)

    async def route_completion(self, request: Request, user_id: str):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})

        model_name = body.get("model", "")
        strategy = body.pop("strategy", "load_balanced")
        stream = body.get("stream", False)
        max_tokens = body.get("max_tokens", 100)
        auth_key = self._get_auth_key(request)

        concurrency_err = await self._acquire_key_slot(auth_key)
        if concurrency_err:
            return concurrency_err

        try:
            budget_err = await self.check_token_budget(auth_key)
            if budget_err:
                return budget_err

            estimated_cost = _estimate_request_cost(max_tokens, model_name)
            compute_err = await self.check_compute_budget(auth_key, estimated_cost)
            if compute_err:
                return compute_err

            # ── Prefix-aware sticky routing for KV cache reuse ──
            conversation_id = body.get("conversation_id")
            prefix_hash = None
            if conversation_id:
                prefix_hash = self._compute_prefix_hash(body)

            # ── Estimate required context length ──
            input_tokens = self._estimate_input_tokens(body)
            min_ctx = input_tokens + max_tokens + 256

            node = await self._select_node(model_name, strategy, prefix_hash=prefix_hash, min_ctx_length=min_ctx)
            if not node:
                return JSONResponse(status_code=503, content={
                    "error": {"message": f"No nodes available for model '{model_name}'"}
                })

            url = f"{node['url'].rstrip('/')}/v1/completions"
            response = await self._forward_request(url, body, stream, node, auth_key, estimated_cost, model_name=model_name)
            await self._record_compute_usage(auth_key, estimated_cost)
            return response
        finally:
            await self._release_key_slot(auth_key)

    async def list_models(self) -> Dict[str, Any]:
        models = self.db.list_active_models()
        return {
            "object": "list",
            "data": [{
                "id": m["model_name"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "llamanet",
                "node_count": m["node_count"],
                "total_tps": m.get("total_tps", 0),
                "avg_load": m.get("avg_load", 0),
                "pool_discovered": m.get("pool_discovered", False),
            } for m in models],
        }

    async def _select_node(self, model_name: str, strategy: str = "load_balanced", prefix_hash: Optional[str] = None, min_ctx_length: int = 0) -> Optional[Dict[str, Any]]:
        from gateway.node_registry import model_name_to_slug

        # Periodic cleanup of stale affinity entries
        self._cleanup_affinity()

        model_slug = model_name_to_slug(model_name)
        nodes = self.db.get_nodes_for_model(model_slug)

        # node_models is the single source of truth — no fallback needed

        if not nodes:
            return None

        # ── Context-length filtering ──
        if min_ctx_length > 0:
            eligible = [
                n for n in nodes
                if (n.get("ctx_length") or 0) >= min_ctx_length
            ]
            if eligible:
                nodes = eligible
                logger.debug(
                    f"Context filter: {len(nodes)} nodes with ctx_length >= {min_ctx_length}"
                )
            else:
                logger.warning(
                    f"No node with ctx_length >= {min_ctx_length} for '{model_name}' "
                    f"(available: {[n.get('ctx_length', 0) for n in nodes]}) — "
                    f"falling back to best available"
                )

        # ── Prefix affinity: sticky routing for KV cache reuse ──
        if prefix_hash and prefix_hash in self._prefix_affinity:
            affinity_node_hash = self._prefix_affinity[prefix_hash]
            for node in nodes:
                if node.get("node_hash") == affinity_node_hash:
                    # Verify affinity node meets context requirement
                    node_ctx = node.get("ctx_length") or 0
                    if min_ctx_length > 0 and node_ctx > 0 and node_ctx < min_ctx_length:
                        logger.debug(
                            f"Affinity node {affinity_node_hash[:8]}... skipped: "
                            f"ctx_length={node_ctx} < required={min_ctx_length}"
                        )
                        break
                    self._affinity_ttl[prefix_hash] = time.time()
                    logger.debug(f"Sticky routing: prefix={prefix_hash[:8]}... → node={affinity_node_hash[:8]}...")
                    return node
            # Affinity node gone or insufficient context — clear stale entry
            logger.debug(f"Affinity node {affinity_node_hash[:8]}... unavailable, re-selecting")
            self._prefix_affinity.pop(prefix_hash, None)
            self._affinity_ttl.pop(prefix_hash, None)

        # ── Normal selection ──
        if strategy == "round_robin":
            idx = self._rr_index.get(model_slug, 0) % len(nodes)
            self._rr_index[model_slug] = idx + 1
            selected = nodes[idx]
        elif strategy == "random":
            import random
            selected = random.choice(nodes)
        else:
            selected = min(nodes, key=lambda n: n.get("load", 1))

        # ── Store affinity for future requests ──
        if prefix_hash and selected:
            self._prefix_affinity[prefix_hash] = selected.get("node_hash", "")
            self._affinity_ttl[prefix_hash] = time.time()
            logger.debug(f"Affinity set: prefix={prefix_hash[:8]}... → node={selected.get('node_hash', '')[:8]}...")

        return selected

    async def _forward_request(self, url: str, body: Dict, stream: bool, node: Dict[str, Any], auth_key: str = "", estimated_cost: float = 0.0, model_name: str = ""):
        headers = {"Content-Type": "application/json"}
        effective_model = model_name or node.get("model_name", "unknown")
        extra_headers = {"X-LLamaNet-Node": node["node_hash"], "X-LLamaNet-Model": effective_model}

        # ── Per-node bearer token for gateway→node auth ──
        node_hash = node.get("node_hash", "")
        if self._node_token_manager:
            node_token = self._node_token_manager.get_token(node_hash)
            if node_token:
                headers["Authorization"] = f"Bearer {node_token}"

        try:
            timeout = aiohttp.ClientTimeout(total=120, connect=10)
            session = aiohttp.ClientSession(timeout=timeout)
            resp = await session.post(url, json=body, headers=headers)
            if resp.status != 200:
                error_text = await resp.text()
                await session.close()
                return JSONResponse(status_code=resp.status, content={
                    "error": {"message": f"Node error: {error_text}", "type": "upstream_error"}
                }, headers=extra_headers)
            if stream:
                return StreamingResponse(
                    self._stream_response(resp, session, auth_key),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Content-Type": "text/event-stream; charset=utf-8",
                        "X-Accel-Buffering": "no",
                        **extra_headers,
                    },
                )
            else:
                data = await resp.json()
                await session.close()
                await self._track_response_tokens(auth_key, data)
                if "node_info" not in data:
                    data["node_info"] = {"node_id": node["node_hash"], "model": effective_model, "processing_node": "routed"}
                return JSONResponse(content=data, headers=extra_headers)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout forwarding to peer {node.get('node_hash', '')[:8]}")
            return JSONResponse(status_code=504, content={"error": {"message": "Node request timed out", "type": "gateway_timeout"}})
        except (aiohttp.ClientConnectionError, OSError) as e:
            node_hash = node.get("node_hash", "")
            logger.warning(f"Node {node_hash[:8]} unreachable: {e}")
            try:
                self.db.deregister_node(node_hash)
            except Exception:
                pass
            return JSONResponse(status_code=503, content={"error": {"message": "Node unreachable — try again shortly", "type": "service_unavailable"}})
        except Exception as e:
            logger.error(f"Error forwarding request: {e}")
            return JSONResponse(status_code=502, content={"error": {"message": f"Failed to reach node: {str(e)}", "type": "bad_gateway"}})

    async def _stream_response(self, resp, session, auth_key: str = ""):
        """Stream response chunks and track token usage from final chunk."""
        final_data = None
        accumulated_content = ""
        try:
            async for chunk in resp.content.iter_any():
                chunk_str = chunk.decode("utf-8", errors="replace")
                for line in chunk_str.split("\n"):
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            parsed = json.loads(line[6:])
                            if parsed.get("usage"):
                                final_data = parsed
                            # Accumulate content for token estimation
                            choices = parsed.get("choices", [])
                            for choice in choices:
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if content:
                                    accumulated_content += content
                        except (json.JSONDecodeError, ValueError):
                            pass
                yield chunk
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await session.close()
            if auth_key:
                if final_data:
                    # Node provided usage data — use it directly
                    await self._track_response_tokens(auth_key, final_data)
                elif accumulated_content:
                    # No usage field in stream — estimate tokens from content
                    estimated_tokens = max(1, len(accumulated_content) // 4)
                    estimated_data = {"usage": {"total_tokens": estimated_tokens, "prompt_tokens": 0, "completion_tokens": estimated_tokens}}
                    await self._track_response_tokens(auth_key, estimated_data)

    async def _track_response_tokens(self, auth_key: str, data: dict):
        """Extract token usage from a response and record it."""
        if not auth_key:
            return
        try:
            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            if total_tokens > 0:
                key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
                self.db.record_token_usage(key_hash, total_tokens)
        except Exception as e:
            logger.debug(f"Token tracking error: {e}")
