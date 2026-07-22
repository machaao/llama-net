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
auth_mgr = None
router = None
registry = None


async def cleanup_stale_loop():
    while True:
        try:
            await asyncio.sleep(60)
            if not registry or not sse_mgr:
                continue
            before = {n["node_hash"]: n for n in supabase_mgr.search_nodes(status="active", limit=500)}
            cleaned = await registry.cleanup_stale()
            if cleaned > 0:
                after = {n["node_hash"] for n in supabase_mgr.search_nodes(status="active", limit=500)}
                for node_hash, node in before.items():
                    if node_hash not in after:
                        await sse_mgr.broadcast("node_left", {
                            "node_hash": node_hash,
                            "model_name": node.get("model_name", "unknown"),
                        })
                        logger.info(f"📡 Broadcast node_left for stale node {node_hash[:8]}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sse_mgr, supabase_mgr, auth_mgr, router, registry
    logger.info("Starting llamanet.app gateway...")
    try:
        supabase_mgr = SupabaseManager()
        auth_mgr = AuthManager(supabase_mgr)
        cf_client = CloudflareClient()
        registry = NodeRegistry(supabase_mgr, cf_client)
        router = ModelRouter(supabase_mgr)
        sse_mgr = GatewaySSEManager()
        if cf_client.is_configured:
            logger.info("Cloudflare tunnel provisioning enabled")
        else:
            logger.warning("Cloudflare not configured - tunnel provisioning disabled")
        cleanup_task = asyncio.create_task(cleanup_stale_loop())
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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "llamanet-gateway", "timestamp": time.time()}


@app.get("/events/network")
async def network_events():
    if not sse_mgr:
        raise HTTPException(status_code=503, detail="SSE not initialized")
    conn_id, queue = await sse_mgr.add_connection()

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'connection_id': conn_id})}\n\n"
            models = supabase_mgr.list_active_models()
            stats = supabase_mgr.get_network_stats()
            yield f"data: {json.dumps({'type': 'initial_state', 'models': models, 'stats': stats})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
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
async def auth_google():
    supabase_url = os.environ.get("SUPABASE_URL", "")
    redirect_to = os.environ.get("LLAMANET_APP_URL", "https://llamanet.app")
    return JSONResponse({"url": f"{supabase_url}/auth/v1/authorize?provider=google&redirect_to={redirect_to}/auth/callback"})


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
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    return {"keys": supabase_mgr.list_api_keys(user["id"])}


@app.post("/auth/api-keys")
async def create_api_key(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
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
    result = await registry.register_node(
        user_id=user["id"], node_id=body["node_id"], model=body.get("model", "unknown"),
        url=body.get("url", ""), ip=body.get("ip", ""), port=body.get("port", 8000),
        gpu=body.get("gpu", ""), metrics=body.get("metrics", {}),
        enable_tunnel=body.get("enable_tunnel", False),
    )
    return {"success": True, "data": result}


@app.post("/api/nodes/heartbeat")
async def node_heartbeat(request: Request):
    body = await request.json()
    node_hash = body.get("node_hash") or body.get("node_id", "")
    if len(node_hash) > 12:
        import hashlib
        node_hash = hashlib.sha256(node_hash.encode()).hexdigest()[:12]
    return {"success": await registry.heartbeat(node_hash, body.get("metrics", {}))}


@app.delete("/api/nodes/{node_hash}")
async def deregister_node(node_hash: str, request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    return {"success": await registry.deregister(node_hash)}


@app.post("/api/nodes/publish")
async def publish_node(request: Request):
    """Public endpoint for inference nodes to self-register (no user auth required)"""
    try:
        body = await request.json()
        node_id = body.get("node_id")
        if not node_id:
            return JSONResponse(status_code=400, content={"error": "node_id required"})

        node_hash = hashlib.sha256(node_id.encode()).hexdigest()[:12]
        model_name = body.get("model", "unknown")
        model_slug = model_name_to_slug(model_name)
        tunnel_url = body.get("tunnel_url", "")
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
        ).eq("status", "active").execute()
        is_new = len(existing.data) == 0

        result = supabase_mgr.register_node(
            user_id=system_user_id, node_hash=node_hash, model_name=model_name,
            model_slug=model_slug, url=tunnel_url or body.get("url", ""),
            ip=body.get("ip", ""), port=body.get("port", 8000),
            gpu_info=body.get("gpu", ""), metrics=body.get("metrics", {}),
        )

        # Broadcast SSE event
        if sse_mgr:
            event_type = "node_joined" if is_new else "node_updated"
            await sse_mgr.broadcast(event_type, {
                "node_hash": node_hash,
                "model_name": model_name,
                "model_slug": model_slug,
                "url": tunnel_url or body.get("url", ""),
                "ip": body.get("ip", ""),
                "port": body.get("port", 8000),
            })

        logger.info(f"{'Published' if is_new else 'Updated'} node {node_hash} model={model_name}")
        return {"success": True, "node_hash": node_hash}
    except Exception as e:
        logger.error(f"Error in publish_node: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/nodes/unpublish")
async def unpublish_node(request: Request):
    """Public endpoint for inference nodes to signal departure (no user auth required)"""
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


@app.get("/api/nodes/mine")
async def my_nodes(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Authentication required"})
    return {"nodes": supabase_mgr.get_user_nodes(user["id"])}


@app.get("/api/models")
async def list_models():
    models = supabase_mgr.list_active_models()
    return {"models": models, "total": len(models)}


@app.get("/api/models/search")
async def search_models(q: str = "", limit: int = 50):
    models = supabase_mgr.list_active_models()
    return {"models": models, "total": len(models)}


@app.get("/api/models/{model_slug}")
async def get_model_nodes(model_slug: str):
    nodes = supabase_mgr.get_nodes_for_model(model_slug)
    return {"model_slug": model_slug, "nodes": nodes, "total": len(nodes)}


@app.get("/api/network/stats")
async def network_stats():
    return supabase_mgr.get_network_stats()


@app.get("/v1/models")
async def openai_list_models(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
    return await router.list_models()


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
    return await router.route_chat_completion(request, user["id"])


@app.post("/v1/completions")
async def openai_completions(request: Request):
    user = await auth_mgr.get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": {"message": "API key required. Get one at https://llamanet.app"}})
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
