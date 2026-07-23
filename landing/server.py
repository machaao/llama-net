import os
import time
import asyncio
import hashlib
import json
import uuid
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from common.utils import get_logger
from common.rate_limiter import RateLimiter
from common.request_validator import RequestValidator, ValidationError
from landing.supabase_client import SupabaseManager
from landing.auth import AuthManager
from landing.node_registry import NodeRegistry, CloudflareClient, model_name_to_slug
from landing.router import ModelRouter

logger = get_logger(__name__)


class GatewaySSEManager:
    """Lightweight SSE manager for real-time landing page updates"""

    def __init__(self):
        self.connections = {}

    async def add_connection(self):
        conn_id = f"sse_{uuid.uuid4().hex[:8]}"
        queue = asyncio.Queue(maxsize=100)
        self.connections[conn_id] = queue
        logger.info(f"SSE connection added: {conn_id} (total: {len(self.connections)})")
        return conn_id, queue

    async def remove_connection(self, conn_id):
        self.connections.pop(conn_id, None)
        logger.info(f"SSE connection removed: {conn_id} (total: {len(self.connections)})")

    async def broadcast(self, event_type, data):
        if not self.connections:
            return
        event = json.dumps({"type": event_type, "timestamp": time.time(), **data})
        dead = []
        for cid, queue in self.connections.items():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(cid)
        for cid in dead:
            self.connections.pop(cid, None)


supabase_mgr = None
auth_mgr = None
router = None
registry = None
sse_mgr = None
rate_limiter = None


def _sanitize_node(node: dict) -> dict:
    """Remove sensitive fields (url, ip, port) from a node dict for public API responses."""
    sanitized = {k: v for k, v in node.items() if k not in ("url", "ip", "port")}
    return sanitized


def _sanitize_models(models: list) -> list:
    """Sanitize all nodes inside a models list."""
    sanitized = []
    for model in models:
        m = dict(model)
        if "nodes" in m and isinstance(m["nodes"], list):
            m["nodes"] = [_sanitize_node(n) for n in m["nodes"]]
        sanitized.append(m)
    return sanitized


async def _heartbeat_monitor_loop():
    """Monitor heartbeat timestamps and detect stale nodes — reads from Supabase"""
    STALE_THRESHOLD = 45
    while True:
        try:
            await asyncio.sleep(5)
            if not supabase_mgr:
                continue

            active_nodes = supabase_mgr.search_nodes(status="active", limit=500)
            current_time = time.time()

            for node in active_nodes:
                node_hash = node["node_hash"]
                hb_str = node.get("last_heartbeat", "")
                if not hb_str:
                    continue
                try:
                    from datetime import datetime
                    hb_time = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
                    elapsed = current_time - hb_time.timestamp()
                    if elapsed > STALE_THRESHOLD:
                        logger.info(f"🕐 Node {node_hash} stale (no heartbeat for {elapsed:.0f}s)")
                        supabase_mgr.deregister_node(node_hash)
                        if sse_mgr:
                            await sse_mgr.broadcast("node_left", {
                                "node_hash": node_hash,
                                "model_name": node.get("model_name", "unknown"),
                                "reason": "heartbeat_timeout",
                            })
                            logger.info(f"📡 Broadcast node_left for stale node {node_hash[:8]}")
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat monitor error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sse_mgr, supabase_mgr, auth_mgr, router, registry, rate_limiter
    logger.info("Starting llamanet.app gateway...")
    try:
        supabase_mgr = SupabaseManager()
        auth_mgr = AuthManager(supabase_mgr)
        cf_client = CloudflareClient()
        registry = NodeRegistry(supabase_mgr, cf_client)
        router = ModelRouter(supabase_mgr)
        sse_mgr = GatewaySSEManager()
        rate_limiter = RateLimiter(
            key_rpm=60, key_rph=1000, key_concurrent=5,
            ip_rpm=30, ip_rph=300,
            global_rpm=500, global_concurrent=50,
            burst_rps=10, node_publish_rph=30,
            sse_per_ip=5, sse_global=200,
        )
        if cf_client.is_configured:
            logger.info("Cloudflare tunnel provisioning enabled")
        else:
            logger.warning("Cloudflare not configured - tunnel provisioning disabled")
        cleanup_task = asyncio.create_task(_heartbeat_monitor_loop())
        logger.info("✅ Gateway started successfully")
    except Exception as e:
        logger.error(f"Failed to start gateway: {e}")
        raise
    yield
    cleanup_task.cancel()
    logger.info("Gateway stopped")


app = FastAPI(title="LlamaNet Gateway", lifespan=lifespan)

# CORS — allow MACHAAO cloud domains and local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://llamanet.app",
        "http://localhost:8000",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^https?://([a-zA-Z0-9-]+\.)*machaao\.com$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


async def _enforce_rate_limit(request: Request, endpoint_type: str = "api"):
    """Helper to enforce rate limits. Returns None if allowed, JSONResponse if denied."""
    if not rate_limiter:
        return None
    allowed, details = await rate_limiter.check_rate_limit(request, endpoint_type)
    if not allowed:
        retry_after = details.get("retry_after", 60)
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": details.get("message", "Rate limit exceeded."),
                    "type": "rate_limit_error",
                    "code": details.get("limit_type", "rate_limited"),
                }
            },
            headers={"Retry-After": str(int(retry_after) + 1)}
        )
    return None


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": "validation_error", "details": exc.details}},
    )


@app.get("/health")
async def health():
    rate_status = rate_limiter.get_status() if rate_limiter else {}
    return {"status": "ok", "service": "llamanet-gateway", "timestamp": time.time(), "rate_limiter": rate_status}


@app.get("/events/network")
async def network_events(request: Request):
    if not sse_mgr:
        raise HTTPException(status_code=503, detail="SSE not initialized")
    if rate_limiter:
        allowed, details = await rate_limiter.check_sse_connection(request)
        if not allowed:
            return JSONResponse(status_code=429, content={"error": details})
        await rate_limiter.track_sse_open(request)
    conn_id, queue = await sse_mgr.add_connection()

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'connection_id': conn_id})}\n\n"
            models = supabase_mgr.list_active_models()
            stats = supabase_mgr.get_network_stats()

            yield f"data: {json.dumps({'type': 'initial_state', 'models': _sanitize_models(models), 'stats': stats})}\n\n"

            # Send recent heartbeat state for nodes to provide current metrics
            try:
                recent_nodes = supabase_mgr.search_nodes(status="active", limit=50)
                for node in recent_nodes:
                    node_hash = node.get("node_hash", "")
                    yield f"data: {json.dumps({'type': 'node_updated', 'node_hash': node_hash, 'model_name': node.get('model_name', 'unknown'), 'metrics': node.get('metrics', {})})}\n\n"
            except Exception:
                pass

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if rate_limiter:
                await rate_limiter.track_sse_close(request)
            await sse_mgr.remove_connection(conn_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def landing_page():
    path = os.path.join(static_dir, "landing.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"message": "LlamaNet Gateway"})


@app.get("/dashboard")
async def dashboard_page():
    path = os.path.join(static_dir, "dashboard.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "Dashboard not found"})


@app.get("/auth/google")
async def auth_google(request: Request):
    from urllib.parse import quote
    supabase_url = os.environ.get("SUPABASE_URL", "")
    redirect_to = os.environ.get("LLAMANET_APP_URL") or str(request.base_url).rstrip("/")
    callback_url = f"{redirect_to}/auth/callback"
    return JSONResponse({"url": f"{supabase_url}/auth/v1/authorize?provider=google&redirect_to={quote(callback_url, safe='')}"})


@app.get("/auth/callback")
async def auth_callback():
    path = os.path.join(static_dir, "auth-callback.html")
    if os.path.exists(path):
        return FileResponse(path)
    return FileResponse(os.path.join(static_dir, "landing.html"))


@app.get("/auth/me")
async def auth_me(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    return {"user": user}


@app.post("/auth/logout")
async def auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("llamanet_session")
    return response


@app.get("/auth/api-keys")
async def list_api_keys(request: Request):
    limit_resp = await _enforce_rate_limit(request, "auth")
    if limit_resp:
        return limit_resp
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    return {"keys": supabase_mgr.list_api_keys(user["id"])}


@app.post("/auth/api-keys")
async def create_api_key(request: Request):
    limit_resp = await _enforce_rate_limit(request, "auth")
    if limit_resp:
        return limit_resp
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    existing_keys = supabase_mgr.list_api_keys(user["id"])
    active_keys = [k for k in existing_keys if k.get("is_active")]
    if len(active_keys) >= 10:
        return JSONResponse(status_code=429, content={
            "error": {"message": "Maximum 10 active API keys per account. Revoke unused keys first."}
        })
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    result = supabase_mgr.create_api_key(user["id"], body.get("name", "default"))
    return {"success": True, "key": result["key"], "key_prefix": result["key_prefix"]}


@app.delete("/auth/api-keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    if not supabase_mgr.revoke_api_key(user["id"], key_id):
        return JSONResponse(status_code=404, content={"error": "Key not found"})
    return {"success": True}


@app.post("/api/nodes/register")
async def register_node(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    body = await request.json()
    if not body.get("node_id"):
        return JSONResponse(status_code=400, content={"error": "node_id is required"})
    RequestValidator.validate_node_registration(body)
    result = await registry.register_node(
        user_id=user["id"], node_id=body["node_id"], model=body.get("model", "unknown"),
        url=body.get("url", ""), ip=body.get("ip", ""), port=body.get("port", 8000),
        gpu=body.get("gpu", ""), metrics=body.get("metrics", {}),
        enable_tunnel=body.get("enable_tunnel", False),
    )
    return {"success": True, "data": result}


@app.post("/api/nodes/heartbeat")
async def node_heartbeat(request: Request):
    limit_resp = await _enforce_rate_limit(request, "heartbeat")
    if limit_resp:
        return limit_resp
    body = await request.json()
    node_hash = body.get("node_hash") or body.get("node_id", "")
    if len(node_hash) > 12:
        node_hash = hashlib.sha256(node_hash.encode()).hexdigest()[:12]
    metrics = body.get("metrics", {})
    node_url = body.get("url", "")

    # Accept pool models from heartbeat payload (persisted to DB via registry.heartbeat)
    pool_models = body.get("models", [])
    if pool_models and len(pool_models) > 0:
        metrics["pool_models"] = pool_models
        metrics["pool_size"] = len(pool_models)

    # Validate URL before processing
    if node_url:
        is_safe, reason = RequestValidator.validate_node_url(node_url)
        if not is_safe:
            logger.warning(f"Rejected heartbeat URL from {node_hash}: {reason}")
            return JSONResponse(status_code=400, content={"error": f"Invalid URL: {reason}"})

    # Update URL in DB unconditionally (tunnel URL rotation)
    if node_url:
        try:
            result = supabase_mgr.client.table("nodes").update(
                {"url": node_url}
            ).eq("node_hash", node_hash).eq("status", "active").execute()

            if result.data:
                logger.info(f"🔄 Updated URL for node {node_hash}: {node_url}")
            else:
                # Node might not exist yet — re-register
                logger.warning(f"⚠️ URL update returned no rows for {node_hash}, attempting upsert")
                supabase_mgr.client.table("nodes").upsert({
                    "node_hash": node_hash,
                    "url": node_url,
                    "status": "active",
                    "last_heartbeat": "now()",
                }, on_conflict="node_hash").execute()
        except Exception as e:
            logger.error(f"URL update failed for {node_hash}: {e}")

    # Read previous metrics from Supabase for change detection
    should_broadcast = False
    try:
        existing_node = supabase_mgr.client.table("nodes").select("metrics, load, tps").eq(
            "node_hash", node_hash
        ).eq("status", "active").execute()
        if existing_node.data:
            prev = existing_node.data[0]
            prev_load = prev.get("load", 0) or 0
            prev_tps = prev.get("tps", 0) or 0
            new_load = metrics.get("load", 0) or 0
            new_tps = metrics.get("tps", 0) or 0
            for old_val, new_val in [(prev_load, new_load), (prev_tps, new_tps)]:
                if old_val == 0 and new_val == 0:
                    continue
                denom = max(abs(old_val), 0.01)
                if abs(new_val - old_val) / denom > 0.30:
                    should_broadcast = True
                    break
    except Exception as e:
        logger.debug(f"Could not read previous metrics for {node_hash}: {e}")

    if should_broadcast and sse_mgr:
        try:
            node = supabase_mgr.client.table("nodes").select("*").eq(
                "node_hash", node_hash
            ).eq("status", "active").execute()
            if node.data:
                await sse_mgr.broadcast("node_updated", {
                    "node_hash": node_hash,
                    "model_name": node.data[0].get("model_name", "unknown"),
                    "metrics": metrics,
                    "pool_models": pool_models,
                })
        except Exception:
            pass

    return {"success": await registry.heartbeat(node_hash, metrics)}


@app.delete("/api/nodes/{node_hash}")
async def deregister_node(node_hash: str, request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    return {"success": await registry.deregister(node_hash)}


@app.post("/api/nodes/publish")
async def publish_node(request: Request):
    """Public endpoint for inference nodes to self-register (no user auth required)"""
    if rate_limiter:
        allowed, details = await rate_limiter.check_node_publish(request)
        if not allowed:
            retry_after = details.get("retry_after", 60)
            return JSONResponse(
                status_code=429,
                content={"error": {"message": details["message"], "type": "rate_limit_error"}},
                headers={"Retry-After": str(int(retry_after) + 1)}
            )
    content_length = request.headers.get("content-length")
    if content_length:
        RequestValidator.validate_request_body_size(int(content_length))
    try:
        body = await request.json()
        node_id = body.get("node_id")
        if not node_id:
            return JSONResponse(status_code=400, content={"error": "node_id required"})
        RequestValidator.validate_node_registration(body)

        node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        model_name = body.get("model", "unknown")
        model_slug = model_name_to_slug(model_name)
        tunnel_url = body.get("tunnel_url", "")

        # Accept pool models list
        models_list = body.get("models", [model_name])
        reg_metrics = body.get("metrics", {})
        if len(models_list) > 1:
            reg_metrics["pool_models"] = models_list
            reg_metrics["pool_size"] = len(models_list)
        system_user_id = "00000000-0000-0000-0000-000000000000"

        # Ensure system user exists (foreign key requirement)
        try:
            existing_user = supabase_mgr.get_user(system_user_id)
            if not existing_user:
                supabase_mgr.client.table("users").upsert({
                    "id": system_user_id,
                    "email": "system@llamanet.app",
                    "full_name": "LlamaNet System",
                }, on_conflict="id").execute()
                logger.info("Created system user for public node registration")
        except Exception as e:
            logger.warning(f"Could not ensure system user exists: {e}")

        # Check if node already exists to determine event type
        existing = supabase_mgr.client.table("nodes").select("node_hash").eq(
            "node_hash", node_hash
        ).execute()
        is_new = len(existing.data) == 0 or existing.data[0].get("status") != "active"

        result = supabase_mgr.register_node(
            user_id=system_user_id, node_hash=node_hash, model_name=model_name,
            model_slug=model_slug, url=tunnel_url or body.get("url", ""),
            ip=body.get("ip", ""), port=body.get("port", 8000),
            gpu_info=body.get("gpu", ""), metrics=reg_metrics,
        )

        # Broadcast SSE event
        if sse_mgr:
            event_type = "node_joined" if is_new else "node_updated"
            await sse_mgr.broadcast(event_type, {
                "node_hash": node_hash,
                "model_name": model_name,
                "model_slug": model_slug,
                "pool_models": models_list,
            })

        logger.info(f"{'Published' if is_new else 'Updated'} node {node_hash} model={model_name}")
        return {"success": True, "node_hash": node_hash}
    except Exception as e:
        logger.error(f"Error in publish_node: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/nodes/unpublish")
async def unpublish_node(request: Request):
    """Public endpoint for inference nodes to signal departure (no user auth required)"""
    # Skip rate limiting for departure events — nodes must be able to leave
    try:
        body = await request.json()
        node_id = body.get("node_id")
        if not node_id:
            return JSONResponse(status_code=400, content={"error": "node_id required"})

        node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        model_name = body.get("model", "unknown")

        # Mark node as inactive in Supabase
        supabase_mgr.deregister_node(node_hash)

        # Broadcast SSE departure event
        if sse_mgr:
            await sse_mgr.broadcast("node_left", {
                "node_hash": node_hash,
                "model_name": model_name,
                "reason": body.get("reason", "graceful_shutdown"),
            })

        logger.info(f"📡 Node unpublished: {node_hash} model={model_name}")
        return {"success": True, "node_hash": node_hash}
    except Exception as e:
        logger.error(f"Error in unpublish_node: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/nodes/event")
async def publish_node_event(request: Request):
    """Endpoint for inference nodes to publish state-change events (join/leave/update/peer_discovered)"""
    limit_resp = await _enforce_rate_limit(request, "node_event")
    if limit_resp:
        return limit_resp
    try:
        body = await request.json()
        event_type = body.get("event_type", "")
        node_id = body.get("node_id", "")

        if not node_id or not event_type:
            return JSONResponse(status_code=400, content={"error": "node_id and event_type required"})
        if len(node_id) > 200:
            return JSONResponse(status_code=400, content={"error": "node_id too long"})
        url = body.get("url", "")
        if url:
            is_safe, reason = RequestValidator.validate_node_url(url)
            if not is_safe:
                return JSONResponse(status_code=400, content={"error": f"Invalid URL: {reason}"})

        node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        model_name = body.get("model", "unknown")
        model_slug = model_name_to_slug(model_name)

        if event_type == "node_joined":
            system_user_id = "00000000-0000-0000-0000-000000000000"
            try:
                existing_user = supabase_mgr.get_user(system_user_id)
                if not existing_user:
                    supabase_mgr.client.table("users").upsert({
                        "id": system_user_id, "email": "system@llamanet.app", "full_name": "LlamaNet System"
                    }, on_conflict="id").execute()
            except Exception:
                pass

            supabase_mgr.register_node(
                user_id=system_user_id, node_hash=node_hash, model_name=model_name,
                model_slug=model_slug, url=body.get("url", ""), ip=body.get("ip", ""),
                port=body.get("port", 8000), gpu_info=body.get("gpu", ""),
                metrics=body.get("metrics", {})
            )

            event_pool_models = body.get("metrics", {}).get("pool_models", [])

            if sse_mgr:
                await sse_mgr.broadcast("node_joined", {
                    "node_hash": node_hash, "model_name": model_name,
                    "model_slug": model_slug,
                    "pool_models": event_pool_models,
                })
            logger.info(f"📡 Node joined via event: {node_hash} model={model_name}")

        elif event_type == "node_left":
            supabase_mgr.deregister_node(node_hash)

            if sse_mgr:
                await sse_mgr.broadcast("node_left", {
                    "node_hash": node_hash, "model_name": model_name,
                    "reason": body.get("reason", "node_event")
                })
            logger.info(f"📡 Node left via event: {node_hash} model={model_name}")

        elif event_type == "node_updated":
            # Update URL unconditionally (tunnel rotation)
            event_url = body.get("url", "")
            if event_url:
                try:
                    result = supabase_mgr.client.table("nodes").update(
                        {"url": event_url}
                    ).eq("node_hash", node_hash).eq("status", "active").execute()
                    if result.data:
                        logger.info(f"🔄 Updated URL for node {node_hash}: {event_url}")
                    else:
                        logger.warning(f"⚠️ node_updated URL update: no active node found for {node_hash}")
                except Exception as e:
                    logger.error(f"URL update in node_updated failed: {e}")

            # Update model name if it changed (hot-reload)
            new_model = body.get("model", "")
            if new_model and new_model != "unknown":
                existing_node = supabase_mgr.client.table("nodes").select("model_name").eq(
                    "node_hash", node_hash
                ).eq("status", "active").execute()
                if existing_node.data and existing_node.data[0].get("model_name") != new_model:
                    new_slug = model_name_to_slug(new_model)
                    supabase_mgr.client.table("nodes").update({
                        "model_name": new_model,
                        "model_slug": new_slug,
                    }).eq("node_hash", node_hash).eq("status", "active").execute()
                    model_name = new_model
                    model_slug = new_slug
                    logger.info(f"📡 Node {node_hash} model changed → {new_model}")

            supabase_mgr.update_node_metrics(node_hash, body.get("metrics", {}))

            event_pool_models = body.get("metrics", {}).get("pool_models", [])

            if sse_mgr:
                await sse_mgr.broadcast("node_updated", {
                    "node_hash": node_hash, "model_name": model_name,
                    "metrics": body.get("metrics", {}),
                    "pool_models": event_pool_models,
                })

        elif event_type == "peer_discovered":
            if sse_mgr:
                await sse_mgr.broadcast("peer_discovered", {
                    "peer_node_id": body.get("peer_node_id", ""),
                    "peer_url": body.get("peer_url", ""),
                    "peer_model": body.get("peer_model", "unknown"),
                    "discovered_by": node_id
                })
            logger.info(f"📡 Peer discovered via event: {body.get('peer_node_id', '')[:8]}...")

        return {"success": True, "event_type": event_type, "node_hash": node_hash}
    except Exception as e:
        logger.error(f"Error in publish_node_event: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/nodes/notify")
async def handle_peer_notification(request: Request):
    """Endpoint for inference nodes to notify gateway about other peers they've discovered"""
    limit_resp = await _enforce_rate_limit(request, "node_event")
    if limit_resp:
        return limit_resp
    try:
        body = await request.json()
        peers = body.get("peers", [])
        notifier_node_id = body.get("notifier_node_id", "")
        if len(peers) > 50:
            return JSONResponse(status_code=400, content={"error": "Maximum 50 peers per notification"})

        new_peers_count = 0
        for peer in peers:
            peer_url = peer.get("url", "")
            if peer_url:
                is_safe, reason = RequestValidator.validate_node_url(peer_url)
                if not is_safe:
                    logger.warning(f"Rejected peer notification URL: {reason}")
                    continue
            peer_node_id = peer.get("node_id", "")
            peer_model = peer.get("model", "unknown")

            if not peer_url or not peer_node_id:
                continue

            peer_hash = hashlib.sha256(peer_node_id.encode()).hexdigest()[:12]

            existing = supabase_mgr.client.table("nodes").select("node_hash").eq(
                "node_hash", peer_hash
            ).eq("status", "active").execute()

            if not existing.data:
                system_user_id = "00000000-0000-0000-0000-000000000000"
                model_slug = model_name_to_slug(peer_model)

                supabase_mgr.register_node(
                    user_id=system_user_id, node_hash=peer_hash,
                    model_name=peer_model, model_slug=model_slug,
                    url=peer_url, ip=peer.get("ip", ""),
                    port=peer.get("port", 8000),
                    metrics=peer.get("metrics", {})
                )

                if sse_mgr:
                    await sse_mgr.broadcast("node_joined", {
                        "node_hash": peer_hash, "model_name": peer_model,
                        "model_slug": model_slug,
                        "discovered_by": notifier_node_id
                    })

                new_peers_count += 1
                logger.info(f"📡 New peer registered via notification: {peer_hash} model={peer_model}")
            else:
                pass

        return {"success": True, "new_peers_count": new_peers_count}
    except Exception as e:
        logger.error(f"Error in handle_peer_notification: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/nodes/mine")
async def my_nodes(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    return {"nodes": supabase_mgr.get_user_nodes(user["id"])}


@app.get("/api/models")
async def list_models():
    models = supabase_mgr.list_active_models()
    return {"models": _sanitize_models(models), "total": len(models)}


@app.get("/api/models/search")
async def search_models(q: str = "", limit: int = 50):
    models = supabase_mgr.list_active_models()
    return {"models": _sanitize_models(models), "total": len(models)}


@app.get("/api/models/{model_slug}")
async def get_model_nodes(model_slug: str):
    nodes = supabase_mgr.get_nodes_for_model(model_slug)
    return {"model_slug": model_slug, "nodes": [_sanitize_node(n) for n in nodes], "total": len(nodes)}


@app.get("/api/network/stats")
async def network_stats():
    return supabase_mgr.get_network_stats()


@app.get("/v1/models")
async def openai_list_models(request: Request):
    limit_resp = await _enforce_rate_limit(request, "api")
    if limit_resp:
        return limit_resp
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
    return await router.list_models()


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    limit_resp = await _enforce_rate_limit(request, "api")
    if limit_resp:
        return limit_resp
    content_length = request.headers.get("content-length")
    if content_length:
        RequestValidator.validate_request_body_size(int(content_length))
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
    try:
        body = await request.json()
        RequestValidator.validate_chat_request(body)
    except ValidationError:
        raise
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
    return await router.route_chat_completion(request, user["id"])


@app.post("/v1/completions")
async def openai_completions(request: Request):
    limit_resp = await _enforce_rate_limit(request, "api")
    if limit_resp:
        return limit_resp
    content_length = request.headers.get("content-length")
    if content_length:
        RequestValidator.validate_request_body_size(int(content_length))
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
    try:
        body = await request.json()
        RequestValidator.validate_completion_request(body)
    except ValidationError:
        raise
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})
    return await router.route_completion(request, user["id"])


def start_server():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    log_level = os.environ.get("LOG_LEVEL", "info")
    uvicorn_config = uvicorn.Config(
        "landing.server:app", host=host, port=port, log_level=log_level,
        timeout_keep_alive=2, access_log=False, loop="asyncio",
        http="httptools", lifespan="on",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(uvicorn_config)
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Gateway stopped")


if __name__ == "__main__":
    start_server()
