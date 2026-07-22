import os
import time
import asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from common.utils import get_logger
from landing.supabase_client import SupabaseManager
from landing.auth import AuthManager
from landing.node_registry import NodeRegistry, CloudflareClient
from landing.router import ModelRouter

logger = get_logger(__name__)

supabase_mgr = None
auth_mgr = None
router = None
registry = None


async def cleanup_stale_loop():
    while True:
        try:
            await asyncio.sleep(60)
            if registry:
                await registry.cleanup_stale()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supabase_mgr, auth_mgr, router, registry
    logger.info("Starting llamanet.app gateway...")
    try:
        supabase_mgr = SupabaseManager()
        auth_mgr = AuthManager(supabase_mgr)
        cf_client = CloudflareClient()
        registry = NodeRegistry(supabase_mgr, cf_client)
        router = ModelRouter(supabase_mgr)
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
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "llamanet-gateway", "timestamp": time.time()}


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
    uvicorn_config = uvicorn.Config(
        "landing.server:app", host=host, port=port, log_level="info",
        timeout_keep_alive=2, access_log=False, loop="asyncio",
        http="httptools", lifespan="on",
    )
    server = uvicorn.Server(uvicorn_config)
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Gateway stopped")


if __name__ == "__main__":
    start_server()
