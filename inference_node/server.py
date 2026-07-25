import os
import asyncio
import time
import uuid
import hashlib
import uvicorn
import aiohttp
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from common.models import (
    OpenAIModel, OpenAIModelList,
    OpenAICompletionRequest, OpenAIChatCompletionRequest,
    OpenAICompletionResponse, OpenAIChatCompletionResponse,
    OpenAIChoice, OpenAIUsage, OpenAIMessage,
    create_streaming_chat_response, create_streaming_completion_response
)
from common.unified_sse import UnifiedSSEManager

from inference_node.config import InferenceConfig
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.metrics import SystemInfo
from inference_node.heartbeat import HeartbeatManager
from inference_node.request_queue import RequestQueueManager
from inference_node.download_manager import DownloadManager
from inference_node.gateway_client import GatewayClient
from inference_node.event_publisher import GatewayEventPublisher
from inference_node.model_pool import ModelPool
from common.utils import get_logger, get_host_ip, get_tunnel_url_file, resolve_static_dir
from common.rate_limiter import RateLimiter
from common.request_validator import RequestValidator, ValidationError

logger = get_logger(__name__)


async def _run_native_probe(llm_instance):
    """Run a native local inference to measure real TTFT/latency/TPS.

    Called once after model loads, before attempting gateway registration.
    Uses the local model directly — no tunnel, no network, no Cloudflare.
    """
    try:
        start_time = time.time()
        result = await llm_instance.generate_chat_safe(
            messages=[{"role": "user", "content": "Say hi"}],
            max_tokens=10,
            temperature=0.7,
            reasoning=False,
        )
        latency = time.time() - start_time
        ttft = latency  # Non-streaming → TTFT ≈ total latency
        tokens = result.get("tokens_generated", 0)
        tps = tokens / latency if latency > 0 else 0

        probe_metrics = {
            "ttft": round(ttft, 3),
            "latency": round(latency, 3),
            "tps": round(tps, 2),
            "completion_tokens": tokens,
            "probe_success": True,
        }
        logger.info(
            f"🔬 Native probe: ttft={ttft:.3f}s, latency={latency:.3f}s, "
            f"tps={tps:.1f}, tokens={tokens}"
        )
        return probe_metrics
    except Exception as e:
        logger.warning(f"Native probe failed: {e}")
        return {"probe_success": False, "error": str(e)}


# Global variables
config = None
llm = None
system_info = None
heartbeat_manager = None
sse_manager = None
request_queue_manager = None
download_manager = None
gateway_client: Optional[GatewayClient] = None
rate_limiter = None
model_pool = None
_active_sse_tasks: set = set()


def _sanitize_node_for_api(node: dict) -> dict:
    """Remove sensitive fields (url, ip, port) from a node dict for public API responses."""
    return {k: v for k, v in node.items() if k not in ("url", "ip", "port")}

def _get_own_url() -> str:
    """Get this node's public tunnel URL. Always checks fresh sources."""
    # Always check fresh sources first (file/env may have updated URL)
    tunnel_url = os.environ.get("LLAMANET_TUNNEL_URL", "")
    if not tunnel_url:
        tunnel_file = get_tunnel_url_file()
        try:
            if os.path.exists(tunnel_file):
                with open(tunnel_file) as f:
                    url = f.read().strip()
                    if url.startswith("http"):
                        tunnel_url = url
        except Exception:
            pass

    if tunnel_url:
        url = tunnel_url.rstrip("/")
        # Update gateway client if URL changed
        if gateway_client and gateway_client.own_url != url:
            old_url = gateway_client.own_url
            gateway_client.own_url = url
            gateway_client.tunnel_url = url
            if old_url:
                logger.info(f"🔄 Tunnel URL changed: {old_url} → {url}")
            else:
                logger.info(f"🔄 Tunnel URL detected: {url}")
        return url

    # Fallback to gateway client
    if gateway_client and gateway_client.own_url:
        return gateway_client.own_url

    return ""


def _sync_tunnel_url_to_gateway(url: str):
    """Sync discovered tunnel URL back to gateway client."""
    if gateway_client and url and gateway_client.own_url != url:
        gateway_client.own_url = url
        gateway_client.tunnel_url = url
        logger.info(f"🔄 Synced tunnel URL to gateway client: {url}")


# Gateway client handles all peer communication — no DHT, P2P, or discovery needed


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, llm, system_info, heartbeat_manager, sse_manager, request_queue_manager, download_manager, gateway_client, rate_limiter, model_pool

    try:
        config = InferenceConfig()
        logger.info(f"Starting inference node: {config}")

        download_manager = DownloadManager()
        request_queue_manager = RequestQueueManager(max_queue_size=50)
        await request_queue_manager.start()

        rate_limiter = RateLimiter(
            key_rpm=60, key_rph=500, key_concurrent=3,
            ip_rpm=20, ip_rph=200,
            global_rpm=200, global_concurrent=30,
            burst_rps=5, sse_per_ip=3, sse_global=100,
        )

        if config.no_model_mode:
            logger.warning("⚠️  Starting in NO-MODEL MODE")
            system_info = SystemInfo.get_all_info()
            sse_manager = UnifiedSSEManager(f"http://{config.host}:{config.port}")
            await sse_manager.start()
        else:
            llm = LlamaWrapper(config)
            system_info = SystemInfo.get_all_info()

            # Initialize model pool
            from inference_node.model_pool import ModelPool
            model_pool = ModelPool(config)
            model_pool.register(config.model_path, llm, config.model_name)
            logger.info(f"Model pool initialized: {model_pool}")

            # Load pool history
            pool_history = config.load_pool_history()
            if pool_history:
                logger.info(f"Pool history: {len(pool_history)} models previously used")

            # Run native probe to measure local inference performance
            probe_metrics = await _run_native_probe(llm)

            # Per-generation metrics broadcast
            _main_loop = asyncio.get_event_loop()
            llm.metrics_manager._event_loop = _main_loop
            def _on_metrics_updated():
                _main_loop.create_task(_broadcast_current_node_metrics())
            llm.metrics_manager._on_metrics_updated = _on_metrics_updated

            heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
            await heartbeat_manager.start()

            sse_manager = UnifiedSSEManager(f"http://{config.host}:{config.port}")
            await sse_manager.start()

            # Gateway client — replaces DHT + P2P + discovery
            if config.bootstrap_peers:
                peer_url = config.bootstrap_peers.split(",")[0].strip()
                gateway_client = GatewayClient(
                    gateway_url=peer_url,
                    node_id=config.node_id,
                    model_name=config.model_name,
                    port=config.port,
                    metrics_callback=llm.get_metrics,
                    public_ip=config.public_ip,
                    model_pool=model_pool,
                )
                gateway_client.probe_metrics = probe_metrics

        # Defer registration until tunnel URL is available and Uvicorn is serving.
        # _wait_for_tunnel_and_register() handles: tunnel detection → register →
        # heartbeat_loop → peer_refresh_loop → send_event("node_joined").
        if gateway_client:
            asyncio.create_task(_wait_for_tunnel_and_register())

        logger.info("✅ All services started")

    except Exception as e:
        logger.error(f"Failed to start: {e}")
        raise

    yield

    logger.info("🛑 Shutting down...")

    # Cancel all active SSE tasks first to prevent blocking during shutdown
    global _active_sse_tasks
    if _active_sse_tasks:
        logger.info(f"Cancelling {len(_active_sse_tasks)} active SSE connections...")
        for task in _active_sse_tasks:
            task.cancel()
        await asyncio.gather(*_active_sse_tasks, return_exceptions=True)
        _active_sse_tasks.clear()

    if gateway_client:
        try:
            await asyncio.wait_for(gateway_client.unregister(), timeout=8.0)
        except Exception:
            logger.warning("⚠️ Unregister timed out — node may appear stale on gateway")
    if model_pool:
        try:
            config.save_pool_history(model_pool.get_history())
            logger.info("Pool state saved")
        except Exception as e:
            logger.debug(f"Pool save error: {e}")
    if heartbeat_manager:
        try:
            await asyncio.wait_for(heartbeat_manager.stop(), timeout=2.0)
        except Exception:
            pass
    if request_queue_manager:
        try:
            await asyncio.wait_for(request_queue_manager.stop(), timeout=2.0)
        except Exception:
            pass
    if sse_manager:
        try:
            await asyncio.wait_for(sse_manager.stop(), timeout=1.0)
        except Exception:
            pass
    logger.info("✅ Shutdown complete")

async def trigger_post_uvicorn_join():
    """Send join event to gateway after uvicorn is ready."""
    try:
        await asyncio.sleep(3.0)
        if gateway_client:
            await gateway_client.send_event("node_joined")
            logger.info("✅ Join event sent to gateway")
    except Exception as e:
        logger.error(f"Failed to send join event: {e}")


async def _wait_for_tunnel_and_register():
    """Wait for tunnel URL + Uvicorn readiness, then register with gateway.

    This is the SOLE registration path. It waits for:
      1. Tunnel URL to appear (from env, file, or cloudflared log)
      2. Local /health endpoint to respond (Uvicorn is serving)
      3. Then registers with gateway, sends join event, starts heartbeat/peer loops
    """
    if not gateway_client:
        return

    # If already registered (shouldn't happen now, but safety check)
    if gateway_client.registered:
        return

    logger.info("⏳ Waiting for tunnel URL and Uvicorn readiness before registering...")

    # Phase 1: Wait for tunnel URL (up to 90s)
    url = ""
    for i in range(90):
        await asyncio.sleep(1)
        url = _get_own_url()
        if url:
            break

    if not url:
        logger.warning("⚠️ No tunnel URL found after 90s — node not registered with gateway")
        return

    # Phase 2: Wait for Uvicorn to be serving /health (up to 30s)
    port = config.port if config else 8000
    for i in range(30):
        await asyncio.sleep(1)
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession(
                timeout=_aiohttp.ClientTimeout(total=2)
            ) as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    if resp.status == 200:
                        logger.info("✅ Uvicorn is serving — proceeding with registration")
                        break
        except Exception:
            pass
    else:
        logger.warning("⚠️ Uvicorn /health not ready after 30s — registering anyway")

    # Phase 3: Register with gateway
    registered = await gateway_client.register()
    if registered:
        logger.info(f"✅ Registered with gateway: {gateway_client.own_url}")
        await gateway_client.send_event("node_joined")
        logger.info("✅ Join event sent to gateway")
        # Start background tasks (only after successful registration)
        asyncio.create_task(gateway_client.heartbeat_loop())
        asyncio.create_task(gateway_client.peer_refresh_loop())
    elif gateway_client._quality_rejected:
        logger.warning(
            f"🚫 Gateway registration rejected by quality gate: "
            f"{gateway_client._rejection_reason}"
        )
    else:
        logger.warning("⚠️ Gateway registration failed — will retry via heartbeat")
        # Start heartbeat loop anyway so it retries on next cycle
        asyncio.create_task(gateway_client.heartbeat_loop())
        asyncio.create_task(gateway_client.peer_refresh_loop())

app = FastAPI(title="LlamaNet OpenAI-Compatible Inference Node", lifespan=lifespan)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": "validation_error"}},
    )


async def _enforce_node_rate_limit(request: Request):
    """Enforce rate limits on inference node endpoints."""
    if not rate_limiter:
        return None
    allowed, details = await rate_limiter.check_rate_limit(request, "inference")
    if not allowed:
        retry_after = details.get("retry_after", 60)
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": details.get("message", "Rate limit exceeded."),
                    "type": "rate_limit_error",
                }
            },
            headers={"Retry-After": str(int(retry_after) + 1)}
        )
    return None


# CORS — allow MACHAAO cloud domains and local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^https?://([a-zA-Z0-9-]+\.)*machaao\.com$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files — resolve from multiple locations
static_dir = resolve_static_dir()
if static_dir:
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Static files: {static_dir}")
else:
    logger.warning("Static files directory not found — Web UI will not be available")

async def _broadcast_current_node_metrics():
    """Broadcast metrics via SSE after each generation."""
    if not sse_manager or not llm or not config:
        return
    try:
        metrics = llm.get_metrics()
        node_info = {
            "node_id": config.node_id,
            "url": _get_own_url(),
            "model": config.model_name,
            "load": metrics.get("load", 0.0),
            "tps": metrics.get("tps", 0.0),
            "uptime": metrics.get("uptime", 0),
            "ttft": metrics.get("ttft", 0),
            "latency": metrics.get("latency", 0),
            "total_tokens": metrics.get("total_tokens", 0),
            "last_seen": int(time.time()),
        }

        # Include pool info if available
        if model_pool:
            node_info["pool"] = model_pool.get_network_info()

        await sse_manager.broadcast_event("node_updated", {
            "node_info": node_info,
            "timestamp": time.time(),
            "source": "post_generation",
        })
    except Exception as e:
        logger.debug(f"Metrics broadcast error: {e}")

    # Push to gateway
    if gateway_client:
        asyncio.create_task(gateway_client.send_event("node_updated"))

async def _verify_gateway_request(request: Request):
    """Verify that the request came from the gateway via bearer token.
    Returns None if OK, JSONResponse if denied.
    """
    from common.gateway_auth import verify_node_request

    path = request.url.path
    if path not in ("/v1/chat/completions", "/v1/completions"):
        return None

    # Allow localhost and loopback connections (direct access or Cloudflare tunnel proxy)
    client_host = ""
    if request.client:
        client_host = request.client.host
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return None

    auth_header = request.headers.get("Authorization", "")
    is_valid, reason = verify_node_request(auth_header)

    if not is_valid:
        logger.warning(f"❌ Gateway auth rejected: {reason}")
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "Direct access not allowed. Requests must be routed through the gateway.",
                    "type": "forbidden",
                }
            },
        )

    return None


async def _check_node_overload() -> Optional[JSONResponse]:
    """Check if the node is overloaded and should reject new requests."""
    if not llm or not model_pool:
        return None

    try:
        overload_info = llm.metrics_manager.is_overloaded(
            cpu_threshold=90.0,
            memory_threshold=92.0,
        )
        if overload_info.get("is_overloaded"):
            reasons = overload_info.get("overload_reasons", [])
            logger.warning(f"⚠️ 503: Node overloaded ({', '.join(reasons)})")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "Node is currently overloaded. Please try again shortly.",
                        "type": "node_overloaded",
                    }
                },
                headers={"Retry-After": "10"},
            )
    except Exception:
        pass

    return None


# Web UI endpoint
@app.get("/")
async def web_ui():
    """Serve the web UI"""
    index_path = os.path.join(static_dir, "index.html") if static_dir else ""
    
    if os.path.exists(index_path):
        return FileResponse(index_path)
    else:
        return {
            "message": "LlamaNet OpenAI-Compatible Inference Node", 
            "web_ui": "Not available", 
            "endpoints": ["/v1/models", "/v1/completions", "/v1/chat/completions"]
        }

# OpenAI-compatible endpoints only
@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible) — includes all pool models"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    models_data = []
    
    # Build model list from pool (all loaded models)
    if model_pool:
        for slot in model_pool.slots.values():
            model_dict = OpenAIModel(
                id=slot.model_name,
                created=int(time.time()),
                owned_by="llamanet"
            ).dict()
            # Add chat format info if available
            if slot.llm:
                try:
                    template_info = slot.llm.get_chat_template_info()
                    model_dict["chat_format"] = template_info.get("chat_format", "unknown")
                    model_dict["detected_format"] = template_info.get("detected_format", "unknown")
                    model_dict["supports_chat"] = template_info.get("supports_chat", False)
                    model_dict["template_auto_detected"] = template_info.get("template_auto_detected", False)
                    model_dict["supports_reasoning"] = getattr(slot.llm, 'supports_reasoning', False)
                except Exception as e:
                    logger.warning(f"Could not get chat format info for {slot.model_name}: {e}")
            # Mark which model is currently active
            model_dict["is_active"] = (slot.model_name == model_pool.active_model)
            model_dict["size_display"] = ModelPool._format_size(slot.size_bytes) if slot.size_bytes else "Unknown"
            models_data.append(model_dict)
    elif llm:
        # Single model fallback (no pool)
        model_dict = OpenAIModel(
            id=config.model_name,
            created=int(time.time()),
            owned_by="llamanet"
        ).dict()
        try:
            template_info = llm.get_chat_template_info()
            model_dict["chat_format"] = template_info.get("chat_format", "unknown")
            model_dict["detected_format"] = template_info.get("detected_format", "unknown")
            model_dict["supports_chat"] = template_info.get("supports_chat", False)
            model_dict["template_auto_detected"] = template_info.get("template_auto_detected", False)
            model_dict["supports_reasoning"] = getattr(llm, 'supports_reasoning', False)
        except Exception as e:
            logger.warning(f"Could not get chat format info: {e}")
        model_dict["is_active"] = True
        models_data.append(model_dict)
    
    if not models_data:
        models_data.append(
            OpenAIModel(id="no-model", created=int(time.time()), owned_by="llamanet").dict()
        )
    
    return OpenAIModelList(data=models_data)

@app.get("/v1/models/network")
async def list_network_models():
    """List local models on this node only (no remote peers)."""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")

    if config.no_model_mode:
        return {"object": "list", "data": [], "total_models": 0, "total_nodes": 0}

    metrics = llm.get_metrics() if llm else {}
    models_dict: Dict[str, Any] = {}

    # Build pool models list for node metadata
    pool_models_list = []
    if model_pool:
        pool_models_list = list(model_pool.slots.keys())
    elif config.model_name:
        pool_models_list = [config.model_name]

    # Add active model
    if config.model_name:
        models_dict[config.model_name] = {
            "id": config.model_name, "object": "model", "created": int(time.time()),
            "owned_by": "llamanet", "node_count": 1, "nodes": [],
        }
        models_dict[config.model_name]["nodes"].append(_sanitize_node_for_api({
            "node_id": config.node_id,
            "model": config.model_name,
            "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
            "uptime": metrics.get("uptime", 0), "last_seen": int(time.time()),
            "ttft": metrics.get("ttft"), "latency": metrics.get("latency"),
            "total_tokens": metrics.get("total_tokens", 0),
            "gpu_info": system_info.get("gpu") if system_info else "",
            "pool_models": pool_models_list,
            "is_pool_model": len(pool_models_list) > 1,
        }))

    # Add other pool models (non-active slots)
    if model_pool:
        for name, slot in model_pool.slots.items():
            if name == config.model_name:
                continue  # Already added above
            if name not in models_dict:
                models_dict[name] = {
                    "id": name, "object": "model", "created": int(time.time()),
                    "owned_by": "llamanet", "node_count": 1, "nodes": [],
                }
                slot_metrics = slot.metrics.get_performance_metrics()
                models_dict[name]["nodes"].append(_sanitize_node_for_api({
                    "node_id": config.node_id,
                    "model": name,
                    "load": slot_metrics.get("load", 0), "tps": slot_metrics.get("tps", 0),
                    "uptime": slot_metrics.get("uptime", 0), "last_seen": int(time.time()),
                    "ttft": slot_metrics.get("ttft"), "latency": slot_metrics.get("latency"),
                    "total_tokens": slot_metrics.get("total_tokens", 0),
                    "gpu_info": system_info.get("gpu") if system_info else "",
                    "pool_models": pool_models_list,
                    "is_pool_model": True,
                }))

    return {
        "object": "list", "data": list(models_dict.values()),
        "total_models": len(models_dict), "total_nodes": 1,
    }

@app.get("/models/statistics")
async def get_models_statistics():
    """Get statistics for local models only."""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")

    if config.no_model_mode:
        return {
            "network_summary": {
                "total_models": 0, "total_nodes": 0,
                "avg_network_load": 0, "total_network_tps": 0,
                "timestamp": time.time(),
            },
            "models": {},
        }

    metrics = llm.get_metrics() if llm else {}
    local_node = _sanitize_node_for_api({
        "node_id": config.node_id,
        "model": config.model_name,
        "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
        "uptime": metrics.get("uptime", 0), "last_seen": int(time.time()),
        "ttft": metrics.get("ttft"), "latency": metrics.get("latency"),
        "total_tokens": metrics.get("total_tokens", 0),
    })

    models_dict: Dict[str, Any] = {}

    # Active model
    if config.model_name:
        models_dict[config.model_name] = {"nodes": [local_node]}

    # Pool models
    if model_pool:
        for name, slot in model_pool.slots.items():
            if name == config.model_name:
                continue
            slot_metrics = slot.metrics.get_performance_metrics()
            pool_node = _sanitize_node_for_api({
                "node_id": config.node_id,
                "model": name,
                "load": slot_metrics.get("load", 0), "tps": slot_metrics.get("tps", 0),
                "uptime": slot_metrics.get("uptime", 0), "last_seen": int(time.time()),
                "ttft": slot_metrics.get("ttft"), "latency": slot_metrics.get("latency"),
                "total_tokens": slot_metrics.get("total_tokens", 0),
            })
            models_dict[name] = {"nodes": [pool_node]}

    statistics: Dict[str, Any] = {
        "network_summary": {
            "total_models": len(models_dict), "total_nodes": 1,
            "avg_network_load": metrics.get("load", 0),
            "total_network_tps": metrics.get("tps", 0),
            "timestamp": time.time(),
        },
        "models": {},
    }

    for model_name, model_data in models_dict.items():
        nodes = model_data["nodes"]
        statistics["models"][model_name] = {
            "node_count": len(nodes),
            "avg_load": nodes[0].get("load", 0) if nodes else 0,
            "total_tps": nodes[0].get("tps", 0) if nodes else 0,
            "best_node": nodes[0] if nodes else None,
            "availability": "local",
            "nodes": nodes,
        }

    return statistics

@app.post("/v1/completions")
async def create_completion(request: Request, body: OpenAICompletionRequest):
    """Text completion — handle locally or route to best peer."""
    # ── Self-throttling ──
    overload_err = await _check_node_overload()
    if overload_err:
        return overload_err

    limit_resp = await _enforce_node_rate_limit(request)
    if limit_resp:
        return limit_resp
    try:
        RequestValidator.validate_completion_request(body.dict())
    except ValidationError:
        raise
    request = body
    if config and config.no_model_mode and not llm:
        if not gateway_client:
            logger.warning("⚠️ 503: No model loaded AND no gateway_client (no_model_mode)")
            raise HTTPException(status_code=503, detail="No model loaded, no peers available")
        peer = await gateway_client.select_node(
            model=getattr(request, 'model', None),
            strategy=getattr(request, 'strategy', 'load_balanced'),
        )
        if not peer:
            logger.warning(f"⚠️ 503: No peers available for model '{getattr(request, 'model', None)}'")
            raise HTTPException(status_code=503, detail="No peers available for this model")
        return await _forward_request(request.dict(), "completions", peer, stream=request.stream)

    if not llm or not request_queue_manager:
        logger.warning(f"⚠️ 503: Services not initialized — llm={llm is not None}, queue={request_queue_manager is not None}")
        raise HTTPException(status_code=503, detail="Services not initialized")

    async def process(request_data):
        return await _handle_completion_locally_queued(request_data["request"])

    return await request_queue_manager.submit_request(
        request_type="completion",
        request_data={"request": request},
        processor=process,
    )

async def _handle_completion_locally_queued(request: OpenAICompletionRequest):
    """Handle completion locally through the queue system"""
    # Handle prompt (can be string or list)
    if isinstance(request.prompt, list):
        if len(request.prompt) == 0:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        prompt = request.prompt[0]  # Use first prompt for now
    else:
        prompt = request.prompt
    
    # Normalize stop tokens for consistent handling
    stop_tokens = None
    if request.stop:
        if isinstance(request.stop, str):
            stop_tokens = [request.stop] if request.stop.strip() else None
        elif isinstance(request.stop, list):
            stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
            stop_tokens = stop_tokens if stop_tokens else None
        else:
            stop_tokens = None
    
    # Check if target model differs from active and try forwarding
    target_model_name = getattr(request, 'target_model', None) or request.model
    if target_model_name and target_model_name != config.model_name and gateway_client:
        peer = await gateway_client.select_node(model=target_model_name, strategy="load_balanced")
        if peer:
            logger.info(f"Forwarding completion for '{target_model_name}' to peer {peer.get('node_id', '')[:8]}...")
            return await _forward_request(request.dict(), "completions", peer, stream=request.stream)

    # Track actual model name used for inference (not config.model_name)
    active_llm = llm
    if model_pool and target_model_name:
        slot = model_pool.get_for_inference(target_model_name)
        if slot:
            active_llm = slot.llm
    actual_model_name = active_llm.config.model_name if active_llm != llm else config.model_name

    try:
        # Handle streaming with robust error handling
        if request.stream:
            request_id = f"cmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    # Use the thread-safe streaming method
                    async for chunk in llm.generate_stream_safe(
                        prompt=prompt,
                        max_tokens=request.max_tokens or 100,
                        temperature=request.temperature or 0.7,
                        top_p=request.top_p or 0.9,
                        stop=stop_tokens,
                        repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                    ):
                        yield {
                            "text": chunk.get("text", ""),
                            "finished": chunk.get("finished", False)
                        }
                        
                except asyncio.CancelledError:
                    logger.info("Local streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in local streaming: {e}")
            
            # Create node info for streaming
            node_info = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": actual_model_name,
                "processing_node": config.node_id,
                "queued": True
            }
            
            return StreamingResponse(
                create_streaming_completion_response(
                    request_id=request_id,
                    model=actual_model_name,
                    stream_generator=local_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        
        # NON-STREAMING: Use thread-safe method
        result = await llm.generate_safe(
            prompt,
            request.max_tokens or 100,
            request.temperature or 0.7,
            request.top_p or 0.9,
            stop_tokens,
            1.0 + (request.frequency_penalty or 0.0)
        )
        
        # Calculate token counts (approximate)
        prompt_tokens = len(prompt.split())
        completion_tokens = result["tokens_generated"]
        
        # Create response
        choice = OpenAIChoice(
            text=result["text"],
            index=0,
            finish_reason="stop"
        )
        
        usage = OpenAIUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens
        )
        
        # Create node info
        node_info = {
            "node_id": config.node_id,
            "ip": get_host_ip(),
            "port": config.port,
            "model": actual_model_name,
            "processing_node": config.node_id,
            "queued": True
        }
        
        return OpenAICompletionResponse(
            id=f"cmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=actual_model_name,
            choices=[choice],
            usage=usage,
            node_info=node_info
        )
        
    except Exception as e:
        logger.error(f"Local completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _handle_completion_locally(request: OpenAICompletionRequest):
    """Handle completion on this node - NON-BLOCKING (used as fallback)"""
    # Handle prompt (can be string or list)
    if isinstance(request.prompt, list):
        if len(request.prompt) == 0:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        prompt = request.prompt[0]  # Use first prompt for now
    else:
        prompt = request.prompt
    
    # Normalize stop tokens for consistent handling
    stop_tokens = None
    if request.stop:
        if isinstance(request.stop, str):
            stop_tokens = [request.stop] if request.stop.strip() else None
        elif isinstance(request.stop, list):
            stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
            stop_tokens = stop_tokens if stop_tokens else None
        else:
            stop_tokens = None
    
    try:
        # Handle streaming with robust error handling
        if request.stream:
            request_id = f"cmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    # Run streaming in thread to avoid blocking
                    loop = asyncio.get_event_loop()
                    
                    def create_stream():
                        return llm.generate_stream(
                            prompt=prompt,
                            max_tokens=request.max_tokens or 100,
                            temperature=request.temperature or 0.7,
                            top_p=request.top_p or 0.9,
                            stop=stop_tokens,
                            repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                        )
                    
                    # Create the generator in a thread
                    stream_gen = await loop.run_in_executor(None, create_stream)
                    
                    # Yield chunks from the generator in thread executor
                    def get_next_chunk():
                        try:
                            return next(stream_gen)
                        except StopIteration:
                            return None
                    
                    while True:
                        chunk = await loop.run_in_executor(None, get_next_chunk)
                        if chunk is None:
                            break
                        yield {
                            "text": chunk.get("text", ""),
                            "finished": chunk.get("finished", False)
                        }
                        
                except asyncio.CancelledError:
                    logger.info("Local streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in local streaming: {e}")
            
            # Create node info for streaming
            node_info = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": config.model_name,
                "processing_node": "local"
            }
            
            return StreamingResponse(
                create_streaming_completion_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=local_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        
        # NON-STREAMING: Run in thread executor to avoid blocking
        loop = asyncio.get_event_loop()
        
        result = await loop.run_in_executor(
            None,  # Use default thread pool
            llm.generate,
            prompt,
            request.max_tokens or 100,
            request.temperature or 0.7,
            request.top_p or 0.9,
            stop_tokens,
            1.0 + (request.frequency_penalty or 0.0)
        )
        
        # Calculate token counts (approximate)
        prompt_tokens = len(prompt.split())
        completion_tokens = result["tokens_generated"]
        
        # Create response
        choice = OpenAIChoice(
            text=result["text"],
            index=0,
            finish_reason="stop"
        )
        
        usage = OpenAIUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens
        )
        
        # Create node info
        node_info = {
            "node_id": config.node_id,
            "ip": get_host_ip(),
            "port": config.port,
            "model": config.model_name,
            "processing_node": config.node_id
        }
        
        return OpenAICompletionResponse(
            id=f"cmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[choice],
            usage=usage,
            node_info=node_info
        )
        
    except Exception as e:
        logger.error(f"Local completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _forward_completion(request: OpenAICompletionRequest, target_node):
    """Forward completion request to another node."""
    try:
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        url = f"http://{target_node.ip}:{target_node.port}/v1/completions"

        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=request_dict, headers={"Content-Type": "application/json"}) as response:
                if response.status == 200:
                    if request.stream:
                        return StreamingResponse(
                            response.content.iter_any(),
                            media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                        )
                    response_data = await response.json()
                    return JSONResponse(content=response_data)
                else:
                    error_text = await response.text()
                    raise HTTPException(status_code=response.status, detail=error_text)
    except asyncio.TimeoutError:
        logger.error(f"Timeout forwarding to {target_node.node_id[:8]}")
        return await _handle_completion_locally(request)
    except Exception as e:
        logger.error(f"Error forwarding to {target_node.node_id[:8]}: {e}")
        return await _handle_completion_locally(request)

@app.post("/v1/chat/completions")
async def create_chat_completion(request: Request, body: OpenAIChatCompletionRequest):
    """Chat completion — handle locally or route to best peer."""
    # ── Self-throttling ──
    overload_err = await _check_node_overload()
    if overload_err:
        return overload_err

    limit_resp = await _enforce_node_rate_limit(request)
    if limit_resp:
        return limit_resp
    try:
        RequestValidator.validate_chat_request(body.dict())
    except ValidationError:
        raise
    request = body
    if config and config.no_model_mode and not llm:
        if not gateway_client:
            logger.warning("⚠️ 503: No model loaded AND no gateway_client (no_model_mode)")
            raise HTTPException(status_code=503, detail="No model loaded, no peers available")
        peer = await gateway_client.select_node(
            model=getattr(request, 'model', None),
            strategy=getattr(request, 'strategy', 'load_balanced'),
        )
        if not peer:
            logger.warning(f"⚠️ 503: No peers available for model '{getattr(request, 'model', None)}'")
            raise HTTPException(status_code=503, detail="No peers available for this model")
        return await _forward_request(request.dict(), "chat/completions", peer, stream=request.stream)

    if not llm or not request_queue_manager:
        logger.warning(f"⚠️ 503: Services not initialized — llm={llm is not None}, queue={request_queue_manager is not None}")
        raise HTTPException(status_code=503, detail="Services not initialized")

    async def process(request_data):
        return await _handle_chat_completion_locally_queued(request_data["request"])

    return await request_queue_manager.submit_request(
        request_type="chat_completion",
        request_data={"request": request},
        processor=process,
    )

async def _handle_chat_completion_locally_queued(request: OpenAIChatCompletionRequest):
    """Handle chat completion locally through the queue system"""
    # Convert OpenAI messages to our format - ENSURE PROPER FORMAT
    messages = []
    for message in request.messages:
        # Ensure we're passing the right structure to llama-cpp-python
        formatted_message = {
            "role": str(message.role),  # Ensure string type
            "content": str(message.content)  # Ensure string type
        }
        messages.append(formatted_message)
    
    # Validate messages format
    if not messages:
        raise HTTPException(status_code=400, detail="Messages cannot be empty")
    
    # Ensure all messages have required fields
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise HTTPException(status_code=400, detail=f"Message {i} must be a dictionary")
        if "role" not in msg or "content" not in msg:
            raise HTTPException(status_code=400, detail=f"Message {i} missing role or content")
        if not isinstance(msg["role"], str) or not isinstance(msg["content"], str):
            raise HTTPException(status_code=400, detail=f"Message {i} role and content must be strings")
    
    # Determine if reasoning should be enabled
    enable_reasoning = request.reasoning
    if enable_reasoning is None and request.enable_reasoning is not None:
        enable_reasoning = request.enable_reasoning
    if enable_reasoning is None:
        enable_reasoning = True  # Default to enabled
    
    # Prepare stop tokens
    stop_tokens = None
    if request.stop:
        if isinstance(request.stop, str):
            stop_tokens = [request.stop] if request.stop.strip() else None
        elif isinstance(request.stop, list):
            stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
            stop_tokens = stop_tokens if stop_tokens else None
    
    # Route to correct model in pool
    active_llm = llm
    target_model_name = getattr(request, 'target_model', None) or request.model
    if model_pool and target_model_name:
        slot = model_pool.get_for_inference(target_model_name)
        if slot:
            active_llm = slot.llm
            logger.debug(f"Pool routing: using {target_model_name} for inference")
        elif target_model_name != config.model_name and gateway_client:
            # Model not in local pool — try forwarding to a peer
            peer = await gateway_client.select_node(model=target_model_name, strategy="load_balanced")
            if peer:
                logger.info(f"Forwarding request for '{target_model_name}' to peer {peer.get('node_id', '')[:8]}...")
                return await _forward_request(request.dict(), "chat/completions", peer, stream=request.stream)
            else:
                logger.debug(f"Pool routing: {target_model_name} not found locally or on network, using active model")
        else:
            logger.debug(f"Pool routing: using active model")

    # Track actual model name used for inference (not config.model_name)
    actual_model_name = active_llm.config.model_name if active_llm != llm else config.model_name

    try:
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    # Use the thread-safe streaming method
                    async for chunk in active_llm.generate_chat_stream_safe(
                        messages=messages,
                        max_tokens=request.max_tokens or 100,
                        temperature=request.temperature or 0.7,
                        top_p=request.top_p or 0.9,
                        stop=stop_tokens,
                        repeat_penalty=1.0 + (request.frequency_penalty or 0.0),
                        reasoning=enable_reasoning
                    ):
                        yield {
                            "text": chunk.get("text", ""),
                            "reasoning": chunk.get("reasoning", "") or chunk.get("reasoning_content", ""),
                            "reasoning_content": chunk.get("reasoning_content", ""),
                            "finished": chunk.get("finished", False)
                        }
                        
                except asyncio.CancelledError:
                    logger.info("Local chat streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in locally queued chat streaming: {e}")
            
            # Create node info for streaming
            node_info = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": actual_model_name,
                "processing_node": config.node_id,
                "chat_template": "auto",
                "queued": True,
                "reasoning_enabled": enable_reasoning
            }
            
            return StreamingResponse(
                create_streaming_chat_response(
                    request_id=request_id,
                    model=actual_model_name,
                    stream_generator=local_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        
        # NON-STREAMING: Use thread-safe method
        result = await active_llm.generate_chat_safe(
            messages,
            request.max_tokens or 100,
            request.temperature or 0.7,
            request.top_p or 0.9,
            stop_tokens,
            1.0 + (request.frequency_penalty or 0.0),
            reasoning=enable_reasoning
        )
        
        # Calculate token counts
        prompt_tokens = sum(len(msg["content"].split()) for msg in messages)
        completion_tokens = result["tokens_generated"]
        
        # Create response message with reasoning support
        response_message = OpenAIMessage(
            role="assistant",
            content=result["text"].strip()
        )
        
        # Add reasoning content if available
        if result.get("reasoning"):
            response_message.reasoning_content = result["reasoning"]
        
        choice = OpenAIChoice(
            message=response_message,
            index=0,
            finish_reason="stop"
        )
        
        usage = OpenAIUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens
        )
        
        # Create node info
        node_info = {
            "node_id": config.node_id,
            "ip": get_host_ip(),
            "port": config.port,
            "model": actual_model_name,
            "processing_node": config.node_id,
            "chat_template": "auto",
            "queued": True,
            "reasoning_enabled": enable_reasoning
        }
        
        return OpenAIChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=actual_model_name,
            choices=[choice],
            usage=usage,
            node_info=node_info
        )
        
    except Exception as e:
        logger.error(f"Local chat completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _handle_chat_completion_locally(request: OpenAIChatCompletionRequest):
    """Handle chat completion using proper chat templates - NON-BLOCKING"""
    
    # Convert OpenAI messages to our format
    messages = []
    for message in request.messages:
        messages.append({
            "role": message.role,
            "content": message.content
        })
    
    # Determine if reasoning should be enabled
    enable_reasoning = request.reasoning
    if enable_reasoning is None and request.enable_reasoning is not None:
        enable_reasoning = request.enable_reasoning
    if enable_reasoning is None:
        enable_reasoning = True  # Default to enabled
    
    # Prepare stop tokens
    stop_tokens = None
    if request.stop:
        if isinstance(request.stop, str):
            stop_tokens = [request.stop] if request.stop.strip() else None
        elif isinstance(request.stop, list):
            stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
            stop_tokens = stop_tokens if stop_tokens else None
    
    try:
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    # Run streaming in thread to avoid blocking
                    loop = asyncio.get_event_loop()
                    
                    def create_stream():
                        return llm.generate_chat_stream(
                            messages=messages,
                            max_tokens=request.max_tokens or 100,
                            temperature=request.temperature or 0.7,
                            top_p=request.top_p or 0.9,
                            stop=stop_tokens,
                            repeat_penalty=1.0 + (request.frequency_penalty or 0.0),
                            reasoning=enable_reasoning
                        )
                    
                    # Create the generator in a thread
                    stream_gen = await loop.run_in_executor(None, create_stream)
                    
                    # Yield chunks from the generator in thread executor
                    def get_next_chunk():
                        try:
                            return next(stream_gen)
                        except StopIteration:
                            return None
                    
                    while True:
                        chunk = await loop.run_in_executor(None, get_next_chunk)
                        if chunk is None:
                            break
                        yield {
                            "text": chunk.get("text", ""),
                            "reasoning": chunk.get("reasoning", "") or chunk.get("reasoning_content", ""),
                            "reasoning_content": chunk.get("reasoning_content", ""),
                            "finished": chunk.get("finished", False)
                        }
                        
                except asyncio.CancelledError:
                    logger.info("Local chat streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in local chat streaming: {e}")
            
            # Create node info for streaming
            node_info = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": config.model_name,
                "processing_node": "local",
                "chat_template": "auto",  # Indicate template support
                "reasoning_enabled": enable_reasoning
            }
            
            return StreamingResponse(
                create_streaming_chat_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=local_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        
        # NON-STREAMING: Run in thread executor to avoid blocking
        loop = asyncio.get_event_loop()
        
        def generate_with_reasoning():
            return llm.generate_chat(
                messages,
                request.max_tokens or 100,
                request.temperature or 0.7,
                request.top_p or 0.9,
                stop_tokens,
                1.0 + (request.frequency_penalty or 0.0),
                reasoning=enable_reasoning
            )
        
        result = await loop.run_in_executor(None, generate_with_reasoning)
        
        # Calculate token counts
        prompt_tokens = sum(len(msg["content"].split()) for msg in messages)
        completion_tokens = result["tokens_generated"]
        
        # Create response message with reasoning support
        response_message = OpenAIMessage(
            role="assistant",
            content=result["text"].strip()
        )
        
        # Add reasoning content if available
        if result.get("reasoning"):
            response_message.reasoning_content = result["reasoning"]
        
        choice = OpenAIChoice(
            message=response_message,
            index=0,
            finish_reason="stop"
        )
        
        usage = OpenAIUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens
        )
        
        # Create node info
        node_info = {
            "node_id": config.node_id,
            "ip": get_host_ip(),
            "port": config.port,
            "model": config.model_name,
            "processing_node": config.node_id,
            "chat_template": "auto",
            "reasoning_enabled": enable_reasoning
        }
        
        return OpenAIChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[choice],
            usage=usage,
            node_info=node_info
        )
        
    except Exception as e:
        logger.error(f"Local chat completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _forward_chat_completion(request: OpenAIChatCompletionRequest, target_node):
    """Forward chat completion request to another node."""
    try:
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        url = f"http://{target_node.ip}:{target_node.port}/v1/chat/completions"

        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=request_dict, headers={"Content-Type": "application/json"}) as response:
                if response.status == 200:
                    if request.stream:
                        return StreamingResponse(
                            response.content.iter_any(),
                            media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                        )
                    response_data = await response.json()
                    return JSONResponse(content=response_data)
                else:
                    error_text = await response.text()
                    raise HTTPException(status_code=response.status, detail=error_text)
    except asyncio.TimeoutError:
        logger.error(f"Timeout forwarding to {target_node.node_id[:8]}")
        return await _handle_chat_completion_locally(request)
    except Exception as e:
        logger.error(f"Error forwarding to {target_node.node_id[:8]}: {e}")
        return await _handle_chat_completion_locally(request)

async def _forward_request(request_body: dict, endpoint: str, target_peer: dict, stream: bool = False):
    """Forward a request to a peer node."""
    peer_url = target_peer.get("url", "").rstrip("/")
    if not peer_url:
        raise HTTPException(status_code=502, detail="Peer has no URL")

    url = f"{peer_url}/v1/{endpoint}"
    headers = {"Content-Type": "application/json", "X-Forwarded-By": config.node_id if config else "unknown"}

    try:
        timeout = aiohttp.ClientTimeout(total=120, connect=10)
        session = aiohttp.ClientSession(timeout=timeout)
        resp = await session.post(url, json=request_body, headers=headers)

        if resp.status != 200:
            error_text = await resp.text()
            await session.close()
            raise HTTPException(status_code=resp.status, detail=error_text)

        if stream:
            async def safe_stream():
                try:
                    async for chunk in resp.content.iter_any():
                        yield chunk
                except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, ConnectionResetError) as e:
                    logger.warning(f"Peer stream connection closed: {e}")
                    yield f"data: {json.dumps({'error': 'Peer connection closed', 'type': 'stream_error'})}\n\n"
                except Exception as e:
                    logger.error(f"Peer stream error: {e}")
                    yield f"data: {json.dumps({'error': str(e), 'type': 'stream_error'})}\n\n"
                finally:
                    await session.close()

            return StreamingResponse(
                safe_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Content-Type": "text/event-stream; charset=utf-8", "X-Accel-Buffering": "no"},
            )
        else:
            data = await resp.json()
            await session.close()
            return JSONResponse(content=data)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Peer request timed out")
    except aiohttp.ClientConnectionError as e:
        logger.error(f"Failed to connect to peer: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to reach peer: connection error")
    except Exception as e:
        logger.error(f"Error forwarding request: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to reach peer: {str(e)}")

# Status and utility endpoints
@app.get("/models/pool")
async def get_pool_status():
    """Return current pool state"""
    if not model_pool:
        return {"enabled": False, "message": "Pool not available"}
    return model_pool.status()


@app.post("/models/pool/evict")
async def evict_model(request: Request):
    """Manually evict a model from the pool"""
    if not model_pool:
        raise HTTPException(status_code=503, detail="Pool not available")
    body = await request.json()
    model_name = body.get("model_name")
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name required")
    success = model_pool.evict(model_name)
    if not success:
        raise HTTPException(status_code=404, detail=f"Model {model_name} not in pool")
    config.save_pool_history(model_pool.get_history())
    return {"success": success, "pool": model_pool.status()}


@app.get("/models/pool/capacity")
async def get_pool_capacity():
    """Return capacity info for UI"""
    if not model_pool:
        return {"enabled": False}
    info = model_pool.get_network_info()
    info["enabled"] = True
    info["loaded_models"] = list(model_pool.slots.keys())
    info["active_model"] = model_pool.active_model
    return info


@app.get("/status")
async def status():
    if config and config.no_model_mode:
        return {"status": "no_model", "no_model_mode": True, "node_id": config.node_id, "timestamp": time.time()}
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
    metrics = llm.get_metrics()
    return {**metrics, "node_id": config.node_id, "model_name": config.model_name, "timestamp": time.time()}

@app.get("/info")
async def info():
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    if config.no_model_mode:
        return {"node_id": config.node_id, "model": "No Model Loaded", "no_model_mode": True, "system": system_info or {}, "endpoints": ["/models/search", "/models/download", "/models/select"]}
    if not system_info:
        raise HTTPException(status_code=503, detail="Node not initialized")
    return {
        "node_id": config.node_id, "model": config.model_name, "model_path": config.model_path,
        "system": system_info, "openai_compatible": True,
        "chat_template": llm.get_chat_template_info() if llm else {},
        "endpoints": ["/v1/models", "/v1/completions", "/v1/chat/completions"],
    }


@app.get("/health")
async def health():
    if config and config.no_model_mode:
        peer_count = 0
        if gateway_client:
            peers = await gateway_client.get_peers()
            peer_count = len(peers)
        return {"status": "router", "no_model_mode": True, "workers_discovered": peer_count, "timestamp": time.time()}

    metrics = llm.get_metrics() if llm else {}
    heartbeat_health = heartbeat_manager.get_health_status() if heartbeat_manager else {}

    return {
        "status": "ok",
        "llm_loaded": llm is not None,
        "model": config.model_name,
        "timestamp": time.time(),
        "heartbeat": heartbeat_health,
        **metrics,
    }


@app.get("/tunnel/status")
async def tunnel_status():
    """Return tunnel status for the node."""
    tunnel_url = os.environ.get("LLAMANET_TUNNEL_URL", "")
    if not tunnel_url:
        tunnel_file = get_tunnel_url_file()
        try:
            if os.path.exists(tunnel_file):
                with open(tunnel_file) as f:
                    tunnel_url = f.read().strip()
        except Exception:
            pass
    if not tunnel_url and gateway_client:
        tunnel_url = gateway_client.tunnel_url
    active = bool(tunnel_url and tunnel_url.startswith("http"))
    return {
        "active": active,
        "url": tunnel_url or "",
        "type": "cloudflare" if active else "none",
        "peers_count": 0,
    }


@app.get("/events/network")
async def network_events(request: Request):
    if not sse_manager:
        raise HTTPException(status_code=503, detail="SSE not initialized")
    if rate_limiter:
        allowed, details = await rate_limiter.check_sse_connection(request)
        if not allowed:
            return JSONResponse(status_code=429, content={"error": details})
        await rate_limiter.track_sse_open(request)

    async def event_generator():
        global _active_sse_tasks
        connection_id = f"sse_{uuid.uuid4().hex[:8]}"
        task = asyncio.current_task()
        _active_sse_tasks.add(task)

        event_queue = await sse_manager.add_connection(connection_id)
        try:
            yield f"data: {json.dumps({'type': 'connected', 'connection_id': connection_id})}\n\n"
            if llm and config:
                yield f"data: {json.dumps({'type': 'node_joined', 'node_info': {'node_id': config.node_id, 'model': config.model_name}})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            if rate_limiter:
                await rate_limiter.track_sse_close(request)
            _active_sse_tasks.discard(task)
            await sse_manager.remove_connection(connection_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



# ═══════════════════════════════════════════════════════
# MODEL MANAGER ENDPOINTS
# ═══════════════════════════════════════════════════════

@app.get("/models/search")
async def search_models(q: str = "", limit: int = 20):
    """Search Hugging Face for GGUF models"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    try:
        results = await download_manager.search_models(q, limit=limit)
        return {
            "success": True,
            "data": results,
            "query": q,
            "count": len(results),
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Error searching models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models/details/{repo_id:path}")
async def get_model_details(repo_id: str):
    """Get detailed model info including GGUF files"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    try:
        details = await download_manager.get_model_details(repo_id)
        return {
            "success": True,
            "data": details,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Error getting model details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/models/download")
async def start_model_download(request: Request):
    """Start downloading a model"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    try:
        body = await request.json()
        repo_id = body.get("repo_id")
        quantization = body.get("quantization", "Q4_K_M")
        
        if not repo_id:
            raise HTTPException(status_code=400, detail="repo_id is required")
        
        download_id = await download_manager.start_download(repo_id, quantization)
        
        return {
            "success": True,
            "data": {
                "download_id": download_id,
                "repo_id": repo_id,
                "quantization": quantization,
                "status": "pending"
            },
            "message": f"Download started for {repo_id}:{quantization}",
            "timestamp": time.time()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models/download/status")
async def download_status_stream(download_id: str = ""):
    """SSE stream for download progress"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    if not download_id:
        return download_manager.get_all_downloads()
    
    async def event_generator():
        queue = download_manager.register_progress_listener(download_id)
        if not queue:
            status = download_manager.get_download_status(download_id)
            if status:
                yield f"data: {json.dumps(status)}\n\n"
            return
        
        # Send current status immediately so client has initial state
        current_status = download_manager.get_download_status(download_id)
        if current_status:
            yield f"data: {json.dumps(current_status)}\n\n"
        
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    
                    if event.get("status") in ("completed", "failed", "cancelled"):
                        break
                        
                except asyncio.TimeoutError:
                    heartbeat = {"type": "heartbeat", "timestamp": time.time()}
                    yield f"data: {json.dumps(heartbeat)}\n\n"
                    
        except asyncio.CancelledError:
            pass
        finally:
            try:
                queue.task_done()
            except Exception:
                pass
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Accel-Buffering": "no",
        }
    )


@app.delete("/models/download/{download_id}")
async def cancel_download(download_id: str):
    """Cancel an active download"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    success = await download_manager.cancel_download(download_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Download {download_id} not found")
    
    return {
        "success": True,
        "message": f"Download {download_id} cancelled",
        "timestamp": time.time()
    }


@app.get("/models/local")
async def list_local_models():
    """List all locally cached models"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    models = download_manager.list_local_models()
    disk = download_manager.get_disk_usage()
    
    return {
        "success": True,
        "data": models,
        "disk_usage": disk,
        "timestamp": time.time()
    }


@app.delete("/models/local/{model_id:path}")
async def delete_local_model_endpoint(model_id: str):
    """Delete a locally cached model"""
    if not download_manager:
        raise HTTPException(status_code=503, detail="Download manager not initialized")
    
    success = download_manager.delete_local_model(model_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")
    
    return {
        "success": True,
        "message": f"Model {model_id} deleted",
        "timestamp": time.time()
    }


@app.post("/models/select")
async def select_model(request: Request):
    """Select a model — instant switch if in pool, otherwise load (may evict LRU)."""
    global llm, gateway_client

    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")

    try:
        body = await request.json()
        model_path = body.get("model_path")
        load_mode = body.get("load_mode", "pool")  # "pool" or "replace"

        if not model_path:
            raise HTTPException(status_code=400, detail="model_path is required")

        if not os.path.exists(model_path):
            raise HTTPException(status_code=404, detail=f"Model file not found: {model_path}")

        model_name = os.path.basename(model_path)

        # ── Pool mode: instant switch or load with LRU eviction ──
        if model_pool and load_mode == "pool":
            existing = model_pool.get(model_name)
            if existing:
                # INSTANT SWITCH
                model_pool.activate(model_name)
                llm = existing.llm
                config.model_name = model_name
                config.model_path = model_path
                config.save_active_model(model_path, model_name)

                if gateway_client:
                    gateway_client.model_name = model_name
                    asyncio.create_task(gateway_client.send_event("node_updated"))

                if sse_manager:
                    metrics = llm.get_metrics()
                    await sse_manager.broadcast_event("node_updated", {
                        "node_info": {
                            "node_id": config.node_id,
                            "url": _get_own_url(),
                            "model": model_name,
                            "load": metrics.get("load", 0),
                            "tps": metrics.get("tps", 0),
                            "pool": model_pool.get_network_info(),
                        },
                        "timestamp": time.time(),
                        "source": "instant_switch",
                    })

                return {
                    "success": True,
                    "data": {
                        "model_path": model_path,
                        "model_name": model_name,
                        "mode": "instant_switch",
                        "reloaded": False,
                        "pool": model_pool.status(),
                    },
                    "message": f"Switched to {model_name} (instant)",
                    "timestamp": time.time(),
                }

            # Not in pool — load it (may evict LRU)
            evicted_name = model_pool.get_eviction_target()
            eviction_warning = None
            if len(model_pool.slots) >= model_pool.max_models and evicted_name:
                eviction_warning = evicted_name

            # In no-model mode, do full initialization
            if config.no_model_mode:
                config.model_path = model_path
                config.model_name = model_name
                config.no_model_mode = False
                config.save_active_model(model_path, model_name)

                new_slot = model_pool.load_model(model_path, model_name)
                llm = new_slot.llm

                # Run native probe after model loads
                probe_metrics = await _run_native_probe(llm)

                if gateway_client:
                    gateway_client.model_name = model_name
                    gateway_client.probe_metrics = probe_metrics

                try:
                    heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
                    await heartbeat_manager.start()
                except Exception as e:
                    logger.warning(f"Failed to start heartbeat manager: {e}")

                if config.bootstrap_peers:
                    try:
                        peer_url = config.bootstrap_peers.split(",")[0].strip()
                        gateway_client_local = GatewayClient(
                            gateway_url=peer_url,
                            node_id=config.node_id,
                            model_name=config.model_name,
                            port=config.port,
                            metrics_callback=llm.get_metrics,
                            public_ip=config.public_ip,
                            model_pool=model_pool,
                        )
                        gateway_client_local.probe_metrics = probe_metrics
                        await gateway_client_local.register()
                        asyncio.create_task(gateway_client_local.heartbeat_loop())
                        asyncio.create_task(gateway_client_local.peer_refresh_loop())
                        gateway_client = gateway_client_local
                    except Exception as e:
                        logger.warning(f"Failed to register with gateway: {e}")

                config.save_pool_history(model_pool.get_history())

                return {
                    "success": True,
                    "data": {
                        "model_path": model_path,
                        "model_name": model_name,
                        "mode": "initial_load",
                        "reloaded": True,
                        "evicted": eviction_warning,
                        "pool": model_pool.status(),
                    },
                    "message": f"Model loaded: {model_name}",
                    "timestamp": time.time(),
                }

            # Normal mode — load into pool
            if not llm:
                raise HTTPException(status_code=503, detail="LLM wrapper not initialized")

            await request_queue_manager.set_reloading(True)
            try:
                drained = await request_queue_manager.drain_active_requests(timeout=30.0)
                if not drained:
                    logger.warning("Not all requests drained - proceeding anyway")

                new_slot = model_pool.load_model(model_path, model_name)
                llm = new_slot.llm
                config.model_name = model_name
                config.model_path = model_path

                # Run native probe after model loads
                probe_metrics = await _run_native_probe(llm)

                if gateway_client:
                    gateway_client.model_name = model_name
                    gateway_client.probe_metrics = probe_metrics
                    asyncio.create_task(gateway_client.send_event("node_updated"))

                config.save_active_model(model_path, model_name)
                config.save_pool_history(model_pool.get_history())

                if sse_manager:
                    try:
                        metrics = llm.get_metrics()
                        await sse_manager.broadcast_event("node_updated", {
                            "node_info": {
                                "node_id": config.node_id,
                                "url": _get_own_url(),
                                "model": model_name,
                                "load": metrics.get("load", 0),
                                "tps": metrics.get("tps", 0),
                                "pool": model_pool.get_network_info(),
                            },
                            "timestamp": time.time(),
                            "source": "pool_load",
                        })
                    except Exception as e:
                        logger.warning(f"Failed to broadcast model change: {e}")

                return {
                    "success": True,
                    "data": {
                        "model_path": model_path,
                        "model_name": model_name,
                        "mode": "pool_load",
                        "reloaded": True,
                        "evicted": eviction_warning,
                        "drained": drained,
                        "pool": model_pool.status(),
                    },
                    "message": f"Model loaded into pool: {model_name}" +
                               (f" (evicted {eviction_warning})" if eviction_warning else ""),
                    "timestamp": time.time(),
                }
            finally:
                await request_queue_manager.set_reloading(False)

        # ── Legacy replace mode (fallback) ──
        if config.no_model_mode:
            config.model_path = model_path
            config.model_name = model_name
            config.no_model_mode = False
            config.save_active_model(model_path, config.model_name)
            if gateway_client:
                gateway_client.model_name = config.model_name

            llm = LlamaWrapper(config)
            try:
                heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
                await heartbeat_manager.start()
            except Exception as e:
                logger.warning(f"Failed to start heartbeat manager: {e}")

            if config.bootstrap_peers:
                try:
                    peer_url = config.bootstrap_peers.split(",")[0].strip()
                    gateway_client_local = GatewayClient(
                        gateway_url=peer_url,
                        node_id=config.node_id,
                        model_name=config.model_name,
                        port=config.port,
                        metrics_callback=llm.get_metrics,
                        public_ip=config.public_ip,
                        model_pool=model_pool,
                    )
                    await gateway_client_local.register()
                    asyncio.create_task(gateway_client_local.heartbeat_loop())
                    asyncio.create_task(gateway_client_local.peer_refresh_loop())
                    gateway_client = gateway_client_local
                except Exception as e:
                    logger.warning(f"Failed to register with gateway: {e}")

            return {
                "success": True,
                "data": {"model_path": model_path, "model_name": config.model_name, "mode": "initial_load", "reloaded": True},
                "message": f"Model loaded: {config.model_name}",
                "timestamp": time.time(),
            }

        if not llm:
            raise HTTPException(status_code=503, detail="LLM wrapper not initialized")

        if model_path == config.model_path:
            return {
                "success": True,
                "data": {"model_path": model_path, "model_name": config.model_name, "mode": "already_loaded", "reloaded": False},
                "message": f"Model already loaded: {config.model_name}",
                "timestamp": time.time(),
            }

        await request_queue_manager.set_reloading(True)
        try:
            drained = await request_queue_manager.drain_active_requests(timeout=30.0)
            if not drained:
                logger.warning("Not all requests drained - proceeding anyway")

            llm.reload_model(model_path)

            # Run native probe after model loads
            probe_metrics = await _run_native_probe(llm)

            if gateway_client:
                gateway_client.model_name = config.model_name
                gateway_client.own_url = gateway_client.tunnel_url
                gateway_client.probe_metrics = probe_metrics
                asyncio.create_task(gateway_client.send_event("node_updated"))

            config.save_active_model(model_path, config.model_name)

            if sse_manager:
                try:
                    await sse_manager.broadcast_event("node_updated", {
                        "node_info": {
                            "node_id": config.node_id,
                            "url": _get_own_url(),
                            "model": config.model_name,
                            "load": 0.0, "tps": 0.0, "uptime": 0,
                            "last_seen": int(time.time()),
                        },
                        "timestamp": time.time(),
                        "source": "model_reload",
                    })
                except Exception as e:
                    logger.warning(f"Failed to broadcast model change: {e}")

            return {
                "success": True,
                "data": {"model_path": model_path, "model_name": config.model_name, "mode": "hot_reload", "reloaded": True, "drained": drained},
                "message": f"Model hot-reloaded: {config.model_name}",
                "timestamp": time.time(),
            }
        finally:
            await request_queue_manager.set_reloading(False)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error selecting model: {e}")
        if request_queue_manager:
            await request_queue_manager.set_reloading(False)
        raise HTTPException(status_code=500, detail=str(e))


def start_server():
    global config
    if config is None:
        config = InferenceConfig()
    log_level = os.environ.get("LOG_LEVEL", "info")
    uvicorn_config = uvicorn.Config(
        "inference_node.server:app",
        host=config.host, port=config.port, log_level=log_level,
        timeout_keep_alive=2, timeout_graceful_shutdown=5,
        access_log=False, loop="asyncio", http="httptools",
        lifespan="on", proxy_headers=True, forwarded_allow_ips="*",
    )
    server = uvicorn.Server(uvicorn_config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass

def show_help():
    """Show help information"""
    print("""
LlamaNet OpenAI-Compatible Inference Node

Usage:
  llamanet run <huggingface-url> [OPTIONS]
  python -m inference_node.server [OPTIONS]

Commands:
  run <hf-url>    Download and run a model from Hugging Face
                  Example: llamanet run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M

Options:
  --model-path PATH     Path to the GGUF model file (required if not using run command)
  --host HOST          Host to bind the service (default: 0.0.0.0)
  --port PORT          HTTP API port (default: 8000)
  --tunnel             Enable Cloudflare tunnel for public URL
  --node-id ID         Unique node identifier (default: auto-generated)
  --bootstrap-nodes    Comma-separated bootstrap nodes (ip:port)

Hugging Face URL Formats:
  hf.co/user/model                 - Latest version
  hf.co/user/model:Q4_K_M         - Specific quantization
  hf.co/user/model@branch          - Specific branch
  user/model                       - Short format
  user/model:Q4_K_M               - Short format with quantization

Examples:
  # Download and run a model from Hugging Face
  llamanet run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M

  # Download and run with custom options
  llamanet run hf.co/TheBloke/Llama-2-7B-Chat-GGUF:Q4_K_M --port 8080

  # Run bootstrap node with local model
  python -m inference_node.server --model-path ./models/model.gguf

  # Run additional node
  python -m inference_node.server \\
    --model-path ./models/model.gguf \\
    --port 8002 \\
    --bootstrap-peers https://llamanet.app \\
    --tunnel

Environment Variables:
  MODEL_PATH, HOST, PORT, NODE_ID, BOOTSTRAP_PEERS
  (Command line arguments take precedence)

OpenAI-Compatible Endpoints:
  GET  /v1/models                - List available models
  POST /v1/completions          - Text completion
  POST /v1/chat/completions     - Chat completion (with streaming support)

Status Endpoints:
  GET  /status                  - Node status and metrics
  GET  /info                    - Node information
  GET  /health                  - Health check
""")

if __name__ == "__main__":
    start_server()
