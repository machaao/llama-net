import asyncio
import json
import time
import aiohttp
from typing import Dict, Any, Optional, List
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse
from common.utils import get_logger

logger = get_logger(__name__)


class ModelRouter:
    def __init__(self, supabase_manager):
        self.db = supabase_manager
        self._rr_index = {}

    async def route_chat_completion(self, request: Request, user_id: str):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        model_name = body.get("model", "")
        strategy = body.pop("strategy", "load_balanced")
        stream = body.get("stream", False)
        node = await self._select_node(model_name, strategy)
        if not node:
            return JSONResponse(status_code=503, content={"error": {"message": f"No nodes available for model '{model_name}'", "type": "server_error", "code": "no_nodes_available"}})
        logger.info(f"Routing chat completion for '{model_name}' to {node['node_hash'][:8]}...")
        url = f"{node['url'].rstrip('/')}/v1/chat/completions"
        return await self._forward_request(url, body, stream, node)

    async def route_completion(self, request: Request, user_id: str):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
        model_name = body.get("model", "")
        strategy = body.pop("strategy", "load_balanced")
        stream = body.get("stream", False)
        node = await self._select_node(model_name, strategy)
        if not node:
            return JSONResponse(status_code=503, content={"error": {"message": f"No nodes available for model '{model_name}'"}})
        url = f"{node['url'].rstrip('/')}/v1/completions"
        return await self._forward_request(url, body, stream, node)

    async def list_models(self) -> Dict[str, Any]:
        models = self.db.list_active_models()
        return {
            "object": "list",
            "data": [{"id": m["model_name"], "object": "model", "created": int(time.time()), "owned_by": "llamanet", "node_count": m["node_count"], "total_tps": m.get("total_tps", 0), "avg_load": m.get("avg_load", 0)} for m in models],
        }

    async def _select_node(self, model_name: str, strategy: str = "load_balanced") -> Optional[Dict[str, Any]]:
        from landing.node_registry import model_name_to_slug
        from landing.server import _node_pool_models_map  # in-memory pool map

        model_slug = model_name_to_slug(model_name)
        nodes = self.db.get_nodes_for_model(model_slug)

        if not nodes:
            all_models = self.db.list_active_models()
            for m in all_models:
                if model_slug in m["model_slug"] or m["model_slug"] in model_slug:
                    nodes = m.get("nodes", [])
                    break

        if not nodes:
            # Use in-memory pool map (populated by heartbeats/events)
            all_active = self.db.search_nodes(status="active", limit=100)
            nodes = []
            for node in all_active:
                node_hash = node.get("node_hash", "")
                pool_models = _node_pool_models_map.get(node_hash, [])
                pool_slugs = [model_name_to_slug(m) for m in pool_models]
                if model_slug in pool_slugs:
                    nodes.append(node)

        if not nodes:
            return None
        if strategy == "round_robin":
            idx = self._rr_index.get(model_slug, 0) % len(nodes)
            self._rr_index[model_slug] = idx + 1
            return nodes[idx]
        elif strategy == "random":
            import random
            return random.choice(nodes)
        return min(nodes, key=lambda n: n.get("load", 1))

    async def _forward_request(self, url: str, body: Dict, stream: bool, node: Dict[str, Any]):
        headers = {"Content-Type": "application/json"}
        extra_headers = {"X-LLamaNet-Node": node["node_hash"], "X-LLamaNet-Model": node["model_name"]}
        try:
            timeout = aiohttp.ClientTimeout(total=120, connect=10)
            session = aiohttp.ClientSession(timeout=timeout)
            resp = await session.post(url, json=body, headers=headers)
            if resp.status != 200:
                error_text = await resp.text()
                await session.close()
                return JSONResponse(status_code=resp.status, content={"error": {"message": f"Node error: {error_text}", "type": "upstream_error"}}, headers=extra_headers)
            if stream:
                return StreamingResponse(
                    self._stream_response(resp, session), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "Content-Type": "text/event-stream; charset=utf-8", "X-Accel-Buffering": "no", **extra_headers},
                )
            else:
                data = await resp.json()
                await session.close()
                if "node_info" not in data:
                    data["node_info"] = {"node_id": node["node_hash"], "model": node["model_name"], "processing_node": "routed"}
                return JSONResponse(content=data, headers=extra_headers)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout forwarding to peer {node.get('node_hash', '')[:8]}")
            return JSONResponse(status_code=504, content={"error": {"message": "Node request timed out", "type": "gateway_timeout"}})
        except (aiohttp.ClientConnectionError, OSError) as e:
            node_hash = node.get("node_hash", "")
            logger.warning(f"Node {node_hash[:8]} unreachable: {e}")
            # Mark node stale immediately so next request routes elsewhere
            try:
                self.db.deregister_node(node_hash)
            except Exception:
                pass
            return JSONResponse(status_code=503, content={"error": {"message": "Node unreachable — try again shortly", "type": "service_unavailable"}})
        except Exception as e:
            logger.error(f"Error forwarding request: {e}")
            return JSONResponse(status_code=502, content={"error": {"message": f"Failed to reach node: {str(e)}", "type": "bad_gateway"}})

    async def _stream_response(self, resp, session):
        try:
            async for chunk in resp.content.iter_any():
                yield chunk
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await session.close()
