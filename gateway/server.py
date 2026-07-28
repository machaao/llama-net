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
from common.utils import get_logger, resolve_static_dir
from common.rate_limiter import RateLimiter
from common.request_validator import RequestValidator, ValidationError
from gateway.supabase_client import SupabaseManager
from gateway.auth import AuthManager
from gateway.node_registry import NodeRegistry, CloudflareClient, model_name_to_slug
from gateway.router import ModelRouter
from common.gateway_auth import NodeTokenManager
from common.quality_gate import NodeQualityGate

logger = get_logger(__name__)


class GatewaySSEManager:
    """Lightweight SSE manager for real-time gateway page updates"""

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
quality_gate = None
node_token_manager = None


def _sanitize_node(node: dict) -> dict:
    """Remove sensitive and redundant fields from a node dict for public API responses."""
    sanitized = {k: v for k, v in node.items() if k not in (
        "url", "ip", "port",           # network addresses
        "node_token",                   # sensitive bearer token
        "user_id",                      # internal user reference
        "pool_models", "metrics",       # redundant JSONB (now tracked via node_models)
        "id",                           # internal UUID
    )}
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


async def _periodic_stats_broadcast():
    """Broadcast aggregated network stats every 30s so gateway page stays fresh."""
    while True:
        try:
            await asyncio.sleep(30)
            if not sse_mgr or not supabase_mgr:
                continue
            stats = supabase_mgr.get_network_stats()
            await sse_mgr.broadcast("stats_update", {
                "stats": stats,
            })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Periodic stats broadcast error: {e}")


async def _heartbeat_monitor_loop():
    """Monitor heartbeat timestamps and detect stale nodes — reads from Supabase"""
    STALE_THRESHOLD = 90  # 30s heartbeat × 3 misses = 90s before marking stale
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
                            # Derive model_name from node_models junction table
                            stale_model = "unknown"
                            try:
                                nm = supabase_mgr.client.table("node_models").select("model_name").eq(
                                    "node_hash", node_hash
                                ).eq("is_active", True).eq("status", "active").execute()
                                if nm.data:
                                    stale_model = nm.data[0].get("model_name", "unknown")
                            except Exception:
                                pass
                            await sse_mgr.broadcast("node_left", {
                                "node_hash": node_hash,
                                "model_name": stale_model,
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
    global sse_mgr, supabase_mgr, auth_mgr, router, registry, rate_limiter, quality_gate, node_token_manager
    logger.info("Starting llamanet.app gateway...")
    try:
        supabase_mgr = SupabaseManager()
        auth_mgr = AuthManager(supabase_mgr)
        cf_client = CloudflareClient()
        registry = NodeRegistry(supabase_mgr, cf_client)
        node_token_manager = NodeTokenManager(supabase_mgr)
        router = ModelRouter(supabase_mgr, node_token_manager)
        sse_mgr = GatewaySSEManager()
        rate_limiter = RateLimiter(
            key_rpm=60, key_rph=1000, key_concurrent=5,
            ip_rpm=30, ip_rph=300,
            global_rpm=500, global_concurrent=50,
            burst_rps=10, node_publish_rph=30,
            sse_per_ip=5, sse_global=200,
        )
        quality_gate = NodeQualityGate()
        if quality_gate.enabled:
            logger.info(f"🔒 Quality gate active: {quality_gate.get_config_summary()}")
        else:
            logger.info("Quality gate disabled (set LLAMANET_REQUIRE_GPU=true to enable)")
        if cf_client.is_configured:
            logger.info("Cloudflare tunnel provisioning enabled")
        else:
            logger.warning("Cloudflare not configured - tunnel provisioning disabled")
        cleanup_task = asyncio.create_task(_heartbeat_monitor_loop())
        stats_task = asyncio.create_task(_periodic_stats_broadcast())
        logger.info("✅ Gateway started successfully")
    except Exception as e:
        logger.error(f"Failed to start gateway: {e}")
        raise
    yield
    stats_task.cancel()
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

static_dir = resolve_static_dir()
if static_dir:
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Static files: {static_dir}")
else:
    logger.warning("Static files directory not found — gateway page will not be available")


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
                    # Derive model_name from node_models junction table
                    active_model = "unknown"
                    try:
                        nm = supabase_mgr.client.table("node_models").select("model_name").eq(
                            "node_hash", node_hash
                        ).eq("is_active", True).eq("status", "active").execute()
                        if nm.data:
                            active_model = nm.data[0].get("model_name", "unknown")
                    except Exception:
                        pass
                    yield f"data: {json.dumps({'type': 'node_updated', 'node_hash': node_hash, 'model_name': active_model, 'metrics': node.get('metrics', {})})}\n\n"
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
    path = os.path.join(static_dir, "gateway.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"message": "LlamaNet Gateway"})


@app.get("/dashboard")
async def dashboard_page():
    path = os.path.join(static_dir, "dashboard.html")
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "Dashboard not found"})


@app.get("/install.sh")
async def install_mac_linux():
    """Serve macOS/Linux installer script"""
    script_path = os.path.join(static_dir, "scripts", "install.sh")
    if os.path.exists(script_path):
        return FileResponse(
            script_path,
            media_type="application/x-sh",
            headers={"Content-Disposition": "inline; filename=install.sh"},
        )
    return JSONResponse(status_code=404, content={"error": "Installer not found"})


@app.get("/install.ps1")
async def install_windows():
    """Serve Windows PowerShell installer script"""
    script_path = os.path.join(static_dir, "scripts", "install.ps1")
    if os.path.exists(script_path):
        return FileResponse(
            script_path,
            media_type="text/plain",
            headers={"Content-Disposition": "inline; filename=install.ps1"},
        )
    return JSONResponse(status_code=404, content={"error": "Installer not found"})


@app.get("/uninstall.sh")
async def uninstall_mac_linux():
    """Serve macOS/Linux uninstaller script"""
    script_path = os.path.join(static_dir, "scripts", "uninstall.sh")
    if os.path.exists(script_path):
        return FileResponse(
            script_path,
            media_type="application/x-sh",
            headers={"Content-Disposition": "inline; filename=uninstall.sh"},
        )
    return JSONResponse(status_code=404, content={"error": "Uninstaller not found"})


@app.get("/uninstall.ps1")
async def uninstall_windows():
    """Serve Windows PowerShell uninstaller script"""
    script_path = os.path.join(static_dir, "scripts", "uninstall.ps1")
    if os.path.exists(script_path):
        return FileResponse(
            script_path,
            media_type="text/plain",
            headers={"Content-Disposition": "inline; filename=uninstall.ps1"},
        )
    return JSONResponse(status_code=404, content={"error": "Uninstaller not found"})


@app.get("/robots.txt")
async def robots():
    """Serve robots.txt for crawlers"""
    path = os.path.join(static_dir, "robots.txt")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/plain")
    return JSONResponse(
        status_code=404,
        content={"error": "Not found"},
    )


@app.get("/sitemap.xml")
async def sitemap():
    """Serve sitemap for search engines"""
    path = os.path.join(static_dir, "sitemap.xml")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/xml")
    return JSONResponse(
        status_code=404,
        content={"error": "Not found"},
    )


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
    return FileResponse(os.path.join(static_dir, "gateway.html"))


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

    logger.debug(f"📨 Heartbeat received: node={node_hash[:16]}...")
    if len(node_hash) > 12:
        node_hash = hashlib.sha256(node_hash.encode()).hexdigest()[:12]
    metrics = body.get("metrics", {})
    node_url = body.get("url", "")

    # Accept pool models from heartbeat payload
    raw_models = body.get("models", [])
    pool_models = []
    if raw_models and len(raw_models) > 0:
        for m in raw_models:
            if isinstance(m, dict):
                pool_models.append(m)
            elif isinstance(m, str):
                pool_models.append({"name": m, "ctx_length": 0})
        metrics["pool_models"] = pool_models
        metrics["pool_size"] = len(pool_models)
        # Pass ctx_length from the active model (first in pool or separate field)
        active_ctx = body.get("ctx_length", 0)
        if active_ctx > 0:
            metrics["ctx_length"] = active_ctx

    # Validate URL before processing
    if node_url:
        is_safe, reason = RequestValidator.validate_node_url(node_url)
        if not is_safe:
            logger.warning(f"Rejected heartbeat URL from {node_hash}: {reason}")
            return JSONResponse(status_code=400, content={"error": f"Invalid URL: {reason}"})

    # Update URL and IP in DB unconditionally (tunnel URL rotation)
    node_ip = body.get("ip", "")
    update_fields = {}
    if node_url:
        update_fields["url"] = node_url
    if node_ip:
        update_fields["ip"] = node_ip

    if update_fields:
        try:
            result = supabase_mgr.client.table("nodes").update(
                update_fields
            ).eq("node_hash", node_hash).eq("status", "active").execute()

            if result.data:
                logger.info(f"🔄 Updated node {node_hash}: {update_fields}")
            else:
                # Node might not exist yet — re-register
                logger.warning(f"⚠️ Update returned no rows for {node_hash}, attempting upsert")
                supabase_mgr.client.table("nodes").upsert({
                    "node_hash": node_hash,
                    "status": "active",
                    "last_heartbeat": "now()",
                    **update_fields,
                }, on_conflict="node_hash").execute()
        except Exception as e:
            logger.error(f"Node update failed for {node_hash}: {e}")

    # ── Change detection: read PREVIOUS metrics BEFORE upserting new ones ──
    should_broadcast = False
    try:
        prev_load = 0
        prev_tps = 0

        # Read previous node-level metrics
        existing_node = supabase_mgr.client.table("nodes").select("load, tps").eq(
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
            if abs(new_val - old_val) / denom > 0.05:
                should_broadcast = True
                break

        # Also broadcast if pool size changed
        if not should_broadcast and pool_models:
            prev_nm = supabase_mgr.get_node_models(node_hash, status="active")
            if len(prev_nm) != len(pool_models):
                should_broadcast = True
    except Exception as e:
        logger.debug(f"Could not read previous metrics for {node_hash}: {e}")

    # ── NOW upsert node_models with new per-model metrics ──
    if pool_models:
        try:
            active_slug = ""
            try:
                nm_check = supabase_mgr.client.table("node_models").select("model_slug").eq(
                    "node_hash", node_hash
                ).eq("is_active", True).eq("status", "active").limit(1).execute()
                if nm_check.data:
                    active_slug = nm_check.data[0].get("model_slug", "")
            except Exception:
                pass
            supabase_mgr.upsert_node_models(node_hash, pool_models, active_slug)
        except Exception as e:
            logger.debug(f"node_models upsert in heartbeat failed: {e}")

    if should_broadcast and sse_mgr:
        try:
            node = supabase_mgr.client.table("nodes").select("*").eq(
                "node_hash", node_hash
            ).eq("status", "active").execute()
            # Derive model name from node_models junction table
            active_model_name = body.get("model", "unknown")  # fallback to heartbeat payload
            try:
                nm_result = supabase_mgr.client.table("node_models").select("model_name").eq(
                    "node_hash", node_hash
                ).eq("is_active", True).eq("status", "active").execute()
                if nm_result.data:
                    active_model_name = nm_result.data[0].get("model_name", active_model_name)
            except Exception:
                pass
            if node.data:
                await sse_mgr.broadcast("node_updated", {
                    "node_hash": node_hash,
                    "model_name": active_model_name,
                    "metrics": metrics,
                    "pool_models": pool_models,
                })
        except Exception:
            pass

    logger.debug(
        f"💓 Heartbeat: node={node_hash} "
        f"load={metrics.get('load', 0):.2f} tps={metrics.get('tps', 0):.1f} "
        f"pool_size={len(pool_models)} "
        f"broadcast={'yes' if should_broadcast else 'no'}"
    )

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
        tunnel_url = body.get("tunnel_url", "")
        ctx_length = body.get("ctx_length", 0)

        # ── Quality Gate: Hardware Check ──
        hw_passed, hw_reason = quality_gate.evaluate_hardware(
            platform=body.get("platform", ""),
            gpu_info=body.get("gpu", ""),
            tunnel_url=tunnel_url,
            url=body.get("url", ""),
        )
        if not hw_passed:
            logger.info(f"❌ Node {node_hash} rejected by hardware gate: {hw_reason}")
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": "Node does not meet network quality requirements",
                    "reason": hw_reason,
                    "failed_check": "hardware",
                },
            )
        model_name = body.get("model", "unknown")
        # Accept pool models list (objects with ctx_length or legacy strings)
        models_list = body.get("models", [{"name": model_name, "ctx_length": ctx_length}])
        # Normalize models_list to object format
        normalized_models = []
        for m in models_list:
            if isinstance(m, dict):
                normalized_models.append(m)
            elif isinstance(m, str):
                normalized_models.append({"name": m, "ctx_length": 0})
            else:
                normalized_models.append({"name": str(m), "ctx_length": 0})
        models_list = normalized_models if normalized_models else [{"name": model_name, "ctx_length": ctx_length}]
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

        model_slug = model_name_to_slug(model_name)
        result = supabase_mgr.register_node(
            user_id=system_user_id, node_hash=node_hash, model_name=model_name,
            model_slug=model_slug, url=tunnel_url or body.get("url", ""),
            ip=body.get("ip", ""), port=body.get("port", 8000),
            gpu_info=body.get("gpu", ""), metrics=reg_metrics,
            ctx_length=ctx_length, models_list=models_list,
        )

        # ── Quality Gate: Performance Check (self-reported native probe metrics) ──
        probe_metrics = body.get("probe_metrics", {})
        if quality_gate.enabled and probe_metrics:
            metrics_passed, metrics_reason = quality_gate.evaluate_metrics(probe_metrics)

            if not metrics_passed:
                logger.info(
                    f"❌ Node {node_hash} rejected by performance gate: {metrics_reason} "
                    f"(ttft={probe_metrics.get('ttft', 0):.2f}, "
                    f"latency={probe_metrics.get('latency', 0):.2f}, "
                    f"tps={probe_metrics.get('tps', 0):.1f})"
                )
                supabase_mgr.deregister_node(node_hash)
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "Node does not meet performance requirements",
                        "reason": metrics_reason,
                        "failed_check": "probe",
                        "probe": {
                            "ttft": round(probe_metrics.get("ttft", 0), 2),
                            "latency": round(probe_metrics.get("latency", 0), 2),
                            "tps": round(probe_metrics.get("tps", 0), 2),
                            "completion_tokens": probe_metrics.get("completion_tokens", 0),
                        },
                    },
                )

            # Persist native probe metrics so the node has real TTFT/latency from day 1
            supabase_mgr.update_node_metrics(node_hash, {
                "ttft": probe_metrics.get("ttft", 0),
                "latency": probe_metrics.get("latency", 0),
                "tps": probe_metrics.get("tps", 0),
            })

        # ── Issue per-node bearer token ──
        node_token = node_token_manager.generate_token(node_hash)

        # Broadcast SSE event
        if sse_mgr:
            event_type = "node_joined" if is_new else "node_updated"
            await sse_mgr.broadcast(event_type, {
                "node_hash": node_hash,
                "model_name": model_name,
                "pool_models": models_list,
                "ctx_length": ctx_length,
                "load": reg_metrics.get("load", 0),
                "tps": reg_metrics.get("tps", 0),
                "ttft": reg_metrics.get("ttft"),
                "latency": reg_metrics.get("latency"),
                "total_tokens": reg_metrics.get("total_tokens", 0),
            })

        logger.info(f"{'Published' if is_new else 'Updated'} node {node_hash} model={model_name}")
        return {"success": True, "node_hash": node_hash, "node_token": node_token}
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

        # Revoke per-node bearer token
        node_token_manager.revoke_token(node_hash)

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

        logger.info(
            f"📨 Node event: type={event_type} node={node_id[:16]}... "
            f"model={body.get('model', '?')} "
            f"pool_size={len(body.get('models', []))}"
        )

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

        if event_type == "node_joined":
            ctx_length = body.get("ctx_length", 0)
            # ── Quality Gate: Hardware Check ──
            hw_passed, hw_reason = quality_gate.evaluate_hardware(
                platform=body.get("platform", ""),
                gpu_info=body.get("gpu", ""),
                tunnel_url=body.get("url", ""),
                url=body.get("url", ""),
            )
            if not hw_passed:
                logger.info(f"❌ Node {node_hash} rejected by hardware gate (event): {hw_reason}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "Node does not meet network quality requirements",
                        "reason": hw_reason,
                        "failed_check": "hardware",
                    },
                )

            system_user_id = "00000000-0000-0000-0000-000000000000"
            try:
                existing_user = supabase_mgr.get_user(system_user_id)
                if not existing_user:
                    supabase_mgr.client.table("users").upsert({
                        "id": system_user_id, "email": "system@llamanet.app", "full_name": "LlamaNet System"
                    }, on_conflict="id").execute()
            except Exception:
                pass

            model_slug = model_name_to_slug(model_name)
            supabase_mgr.register_node(
                user_id=system_user_id, node_hash=node_hash, model_name=model_name,
                model_slug=model_slug, url=body.get("url", ""), ip=body.get("ip", ""),
                port=body.get("port", 8000), gpu_info=body.get("gpu", ""),
                metrics=body.get("metrics", {}),
                ctx_length=ctx_length,
            )

            # Upsert node_models for pool models
            event_pool_models_raw = body.get("models", [])
            event_pool_models = []
            for m in event_pool_models_raw:
                if isinstance(m, dict):
                    event_pool_models.append(m)
                elif isinstance(m, str):
                    event_pool_models.append({"name": m, "ctx_length": 0})

            logger.info(
                f"🟢 NODE JOINED: {node_hash} model={model_name} "
                f"url={body.get('url', '')[:60]} "
                f"ctx={ctx_length} "
                f"pool={[m.get('name', m) if isinstance(m, dict) else m for m in event_pool_models]} "
                f"load={body.get('metrics', {}).get('load', 0):.2f} "
                f"tps={body.get('metrics', {}).get('tps', 0):.1f} "
                f"ttft={body.get('metrics', {}).get('ttft', 'N/A')} "
                f"latency={body.get('metrics', {}).get('latency', 'N/A')} "
                f"gpu={body.get('gpu', '')[:40]}"
            )

            if event_pool_models:
                try:
                    supabase_mgr.upsert_node_models(node_hash, event_pool_models, model_name_to_slug(model_name))
                except Exception as e:
                    logger.debug(f"node_models upsert in event failed: {e}")

            # ── Quality Gate: Performance Check (self-reported native probe metrics) ──
            event_probe_metrics = body.get("probe_metrics", {})
            if quality_gate.enabled and event_probe_metrics:
                metrics_passed, metrics_reason = quality_gate.evaluate_metrics(event_probe_metrics)

                if not metrics_passed:
                    logger.info(
                        f"❌ Node {node_hash} rejected by performance gate (event): {metrics_reason}"
                    )
                    supabase_mgr.deregister_node(node_hash)
                    return JSONResponse(
                        status_code=403,
                        content={
                            "success": False,
                            "error": "Node does not meet performance requirements",
                            "reason": metrics_reason,
                            "failed_check": "probe",
                            "probe": {
                                "ttft": round(event_probe_metrics.get("ttft", 0), 2),
                                "latency": round(event_probe_metrics.get("latency", 0), 2),
                                "tps": round(event_probe_metrics.get("tps", 0), 2),
                                "completion_tokens": event_probe_metrics.get("completion_tokens", 0),
                            },
                        },
                    )

                supabase_mgr.update_node_metrics(node_hash, {
                    "ttft": event_probe_metrics.get("ttft", 0),
                    "latency": event_probe_metrics.get("latency", 0),
                    "tps": event_probe_metrics.get("tps", 0),
                })

            if sse_mgr:
                await sse_mgr.broadcast("node_joined", {
                    "node_hash": node_hash, "model_name": model_name,
                    "pool_models": event_pool_models,
                    "ctx_length": body.get("ctx_length", 0),
                    "load": body.get("metrics", {}).get("load", 0),
                    "tps": body.get("metrics", {}).get("tps", 0),
                    "ttft": body.get("metrics", {}).get("ttft"),
                    "latency": body.get("metrics", {}).get("latency"),
                    "total_tokens": body.get("metrics", {}).get("total_tokens", 0),
                })
            logger.info(f"📡 Node joined via event: {node_hash} model={model_name}")

        elif event_type == "node_left":
            supabase_mgr.deregister_node(node_hash)

            logger.info(
                f"🔴 NODE LEFT: {node_hash} model={model_name} "
                f"reason={body.get('reason', 'node_event')}"
            )

            if sse_mgr:
                await sse_mgr.broadcast("node_left", {
                    "node_hash": node_hash, "model_name": model_name,
                    "reason": body.get("reason", "node_event")
                })
            logger.info(f"📡 Node left via event: {node_hash} model={model_name}")

        elif event_type == "node_updated":
            # Update URL and IP unconditionally (tunnel rotation)
            event_url = body.get("url", "")
            event_ip = body.get("ip", "")
            update_fields = {}
            if event_url:
                update_fields["url"] = event_url
            if event_ip:
                update_fields["ip"] = event_ip

            if update_fields:
                try:
                    result = supabase_mgr.client.table("nodes").update(
                        update_fields
                    ).eq("node_hash", node_hash).eq("status", "active").execute()
                    if result.data:
                        logger.info(f"🔄 Updated node {node_hash}: {update_fields}")
                    else:
                        logger.warning(f"⚠️ node_updated: no active node found for {node_hash}")
                except Exception as e:
                    logger.error(f"Node update in node_updated failed: {e}")

            # Extract metrics early for logging
            event_metrics = body.get("metrics", {})
            event_ctx_length = body.get("ctx_length", 0)

            logger.info(
                f"🔄 NODE UPDATED: {node_hash} model={model_name} "
                f"pool={[m.get('name', '?') for m in (event_metrics.get('pool_models', []) or body.get('models', []))]} "
                f"load={event_metrics.get('load', 0):.2f} "
                f"tps={event_metrics.get('tps', 0):.1f} "
                f"ttft={event_metrics.get('ttft', 'N/A')} "
                f"total_tokens={event_metrics.get('total_tokens', 0)}"
            )

            # Model changes are tracked via node_models junction table (is_active flag)
            # No need to update nodes table directly
            if event_ctx_length > 0:
                event_metrics["ctx_length"] = event_ctx_length

            # Extract pool_models from multiple possible locations
            raw_pool_models = (
                event_metrics.get("pool_models", [])
                or event_metrics.get("pool", {}).get("models", [])
            )
            if not raw_pool_models:
                raw_pool_models = body.get("models", [])

            # Re-inject ctx_length if it was set from body
            if event_ctx_length > 0:
                event_metrics["ctx_length"] = event_ctx_length

            # Normalize to object format
            event_pool_models = []
            for m in raw_pool_models:
                if isinstance(m, dict):
                    event_pool_models.append(m)
                elif isinstance(m, str):
                    event_pool_models.append({"name": m, "ctx_length": 0})

            # Always track pool state — even when empty
            event_metrics["pool_models"] = event_pool_models
            event_metrics["pool_size"] = len(event_pool_models)

            if not event_pool_models:
                # Pool drained — mark all node_models as evicted
                try:
                    supabase_mgr.client.table("node_models").update({
                        "status": "evicted",
                        "is_active": False,
                        "updated_at": "now()",
                    }).eq("node_hash", node_hash).eq("status", "active").execute()
                    logger.info(f"🧹 Pool empty — evicted all node_models for {node_hash}")
                except Exception as e:
                    logger.debug(f"Pool empty eviction failed: {e}")

            if event_pool_models:
                # Active model is tracked via node_models.is_active — no nodes table update needed
                active_model = next(
                    (m for m in event_pool_models if isinstance(m, dict) and m.get("is_active")),
                    None
                )
                if not active_model:
                    active_model = event_pool_models[0] if event_pool_models else None
                if active_model and isinstance(active_model, dict):
                    active_name = active_model.get("name", "")
                    if active_name:
                        model_name = active_name

                # Upsert node_models junction table — preserve existing active slug
                try:
                    existing_slug = ""
                    try:
                        nm_check = supabase_mgr.client.table("node_models").select("model_slug").eq(
                            "node_hash", node_hash
                        ).eq("is_active", True).eq("status", "active").limit(1).execute()
                        if nm_check.data:
                            existing_slug = nm_check.data[0].get("model_slug", "")
                    except Exception:
                        pass
                    supabase_mgr.upsert_node_models(node_hash, event_pool_models, existing_slug)
                except Exception as e:
                    logger.debug(f"node_models upsert in node_updated failed: {e}")

                # Remove evicted models from junction table
                pool_slugs = [
                    model_name_to_slug(m.get("name", ""))
                    for m in event_pool_models if isinstance(m, dict) and m.get("name")
                ]
                try:
                    supabase_mgr.evict_stale_node_models(node_hash, pool_slugs)
                except Exception as e:
                    logger.debug(f"Stale node_models cleanup failed: {e}")

            supabase_mgr.update_node_metrics(node_hash, event_metrics)

            if sse_mgr:
                # Detect empty pool state from node event
                is_pool_empty = (
                    not event_pool_models
                    or body.get("metrics", {}).get("pool_empty", False)
                )

                await sse_mgr.broadcast("node_updated", {
                    "node_hash": node_hash, "model_name": model_name,
                    "metrics": event_metrics,
                    "pool_models": event_pool_models,
                    "pool_empty": is_pool_empty,
                    "no_model_mode": is_pool_empty,
                    "load": event_metrics.get("load", 0),
                    "tps": event_metrics.get("tps", 0),
                    "ttft": event_metrics.get("ttft"),
                    "latency": event_metrics.get("latency"),
                    "total_tokens": event_metrics.get("total_tokens", 0),
                })

        elif event_type == "peer_discovered":
            if sse_mgr:
                await sse_mgr.broadcast("peer_discovered", {
                    "peer_node_id": body.get("peer_node_id", ""),
                    "peer_url": body.get("peer_url", ""),
                    "peer_model": body.get("peer_model", "unknown"),
                    "discovered_by": node_id
                })
            logger.info(
                f"🔵 PEER DISCOVERED: {body.get('peer_node_id', '')[:16]}... "
                f"model={body.get('peer_model', 'unknown')} "
                f"by={node_id[:16]}..."
            )

        logger.info(
            f"✅ Event processed: type={event_type} node={node_hash} model={model_name}"
        )

        return {"success": True, "event_type": event_type, "node_hash": node_hash}
    except Exception as e:
        logger.error(f"Error in publish_node_event (type={body.get('event_type', '?') if 'body' in dir() else '?'}): {e}", exc_info=True)
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

    # ── Token budget pre-check ──
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:].startswith("ln-"):
        key_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
        usage = supabase_mgr.get_token_usage(key_hash)
        budget = supabase_mgr.get_daily_token_budget()
        if usage.get("tokens_consumed", 0) >= budget:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": f"Daily token budget exceeded ({usage['tokens_consumed']:,}/{budget:,} tokens). Resets at midnight UTC.",
                        "type": "token_budget_exceeded",
                    }
                },
            )

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

    # ── Token budget pre-check ──
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:].startswith("ln-"):
        key_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
        usage = supabase_mgr.get_token_usage(key_hash)
        budget = supabase_mgr.get_daily_token_budget()
        if usage.get("tokens_consumed", 0) >= budget:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": f"Daily token budget exceeded ({usage['tokens_consumed']:,}/{budget:,} tokens). Resets at midnight UTC.",
                        "type": "token_budget_exceeded",
                    }
                },
            )

    return await router.route_completion(request, user["id"])


@app.get("/auth/token-usage")
async def get_token_usage(request: Request):
    """Get today's token usage for the authenticated user's API keys."""
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    keys = supabase_mgr.list_api_keys(user["id"])
    budget = supabase_mgr.get_daily_token_budget()
    usage_data = []

    for key in keys:
        try:
            result = supabase_mgr.client.table("api_keys").select("key_hash").eq(
                "id", key["id"]
            ).execute()
            if result.data:
                actual_hash = result.data[0]["key_hash"]
                usage = supabase_mgr.get_token_usage(actual_hash)
                usage_data.append({
                    "key_id": key["id"],
                    "key_prefix": key.get("key_prefix", ""),
                    "name": key.get("name", "default"),
                    "is_active": key.get("is_active", False),
                    "tokens_consumed": usage.get("tokens_consumed", 0),
                    "requests_count": usage.get("requests_count", 0),
                    "budget": budget,
                    "percent": round(
                        (usage.get("tokens_consumed", 0) / budget * 100) if budget > 0 else 0, 1
                    ),
                })
            else:
                usage_data.append({
                    "key_id": key["id"],
                    "key_prefix": key.get("key_prefix", ""),
                    "name": key.get("name", "default"),
                    "is_active": key.get("is_active", False),
                    "tokens_consumed": 0,
                    "requests_count": 0,
                    "budget": budget,
                    "percent": 0,
                })
        except Exception as e:
            logger.debug(f"Token usage lookup error for key {key['id']}: {e}")
            usage_data.append({
                "key_id": key["id"],
                "key_prefix": key.get("key_prefix", ""),
                "name": key.get("name", "default"),
                "is_active": key.get("is_active", False),
                "tokens_consumed": 0,
                "requests_count": 0,
                "budget": budget,
                "percent": 0,
            })

    return {"keys": usage_data, "budget": budget}


def start_server():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    log_level = os.environ.get("LOG_LEVEL", "info")
    uvicorn_config = uvicorn.Config(
        "gateway.server:app", host=host, port=port, log_level=log_level,
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
