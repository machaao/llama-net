import os
import asyncio
import time
import uuid
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
from common.utils import get_logger, get_host_ip

logger = get_logger(__name__)

# Global variables
config = None
llm = None
system_info = None
heartbeat_manager = None
sse_manager = None
request_queue_manager = None
download_manager = None
gateway_client: Optional[GatewayClient] = None

def _get_own_url() -> str:
    """Get this node's public URL."""
    if gateway_client:
        return gateway_client.own_url
    tunnel_url = os.environ.get("LLAMANET_TUNNEL_URL", "")
    if tunnel_url:
        return tunnel_url.rstrip("/")
    return f"http://{get_host_ip()}:{config.port}" if config else "http://localhost:8000"

# Gateway client handles all peer communication — no DHT, P2P, or discovery needed


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, llm, system_info, heartbeat_manager, sse_manager, request_queue_manager, download_manager, gateway_client

    try:
        config = InferenceConfig()
        logger.info(f"Starting inference node: {config}")

        download_manager = DownloadManager()
        request_queue_manager = RequestQueueManager(max_queue_size=50)
        await request_queue_manager.start()

        if config.no_model_mode:
            logger.warning("⚠️  Starting in NO-MODEL MODE")
            system_info = SystemInfo.get_all_info()
            sse_manager = UnifiedSSEManager(f"http://{config.host}:{config.port}")
            await sse_manager.start()
        else:
            llm = LlamaWrapper(config)
            system_info = SystemInfo.get_all_info()

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
                )
                await gateway_client.register()
                asyncio.create_task(gateway_client.heartbeat_loop())
                asyncio.create_task(gateway_client.peer_refresh_loop())

        # Schedule post-startup join event
        if gateway_client:
            asyncio.create_task(trigger_post_uvicorn_join())

        logger.info("✅ All services started")

    except Exception as e:
        logger.error(f"Failed to start: {e}")
        raise

    yield

    logger.info("🛑 Shutting down...")
    if gateway_client:
        try:
            await asyncio.wait_for(gateway_client.unregister(), timeout=5.0)
        except Exception:
            pass
    if heartbeat_manager:
        await heartbeat_manager.stop()
    if request_queue_manager:
        await request_queue_manager.stop()
    if sse_manager:
        await sse_manager.stop()
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

app = FastAPI(title="LlamaNet OpenAI-Compatible Inference Node", lifespan=lifespan)

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

# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

async def _broadcast_current_node_metrics():
    """Broadcast metrics via SSE after each generation."""
    if not sse_manager or not llm or not config:
        return
    try:
        metrics = llm.get_metrics()
        await sse_manager.broadcast_event("node_updated", {
            "node_info": {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": config.model_name,
                "load": metrics.get("load", 0.0),
                "tps": metrics.get("tps", 0.0),
                "uptime": metrics.get("uptime", 0),
                "ttft": metrics.get("ttft", 0),
                "latency": metrics.get("latency", 0),
                "total_tokens": metrics.get("total_tokens", 0),
                "last_seen": int(time.time()),
            },
            "timestamp": time.time(),
            "source": "post_generation",
        })
    except Exception as e:
        logger.debug(f"Metrics broadcast error: {e}")

    # Push to gateway
    if gateway_client:
        asyncio.create_task(gateway_client.send_event("node_updated"))

# Web UI endpoint
@app.get("/")
async def web_ui():
    """Serve the web UI"""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    index_path = os.path.join(static_dir, "index.html")
    
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
    """List available models (OpenAI-compatible) with chat format info"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    # Get chat template info if available
    chat_format_info = {}
    if llm:
        try:
            template_info = llm.get_chat_template_info()
            chat_format_info = {
                "chat_format": template_info.get("chat_format", "unknown"),
                "detected_format": template_info.get("detected_format", "unknown"),
                "supports_chat": template_info.get("supports_chat", False),
                "template_auto_detected": template_info.get("template_auto_detected", False)
            }
        except Exception as e:
            logger.warning(f"Could not get chat format info: {e}")
            chat_format_info = {"chat_format": "unknown", "error": str(e)}
    
    model_data = OpenAIModel(
        id=config.model_name,
        created=int(time.time()),
        owned_by="llamanet"
    )
    
    # Add chat format info to the model data
    model_dict = model_data.dict()
    model_dict.update(chat_format_info)
    
    return OpenAIModelList(data=[model_dict])

@app.get("/v1/models/network")
async def list_network_models():
    """List all available models across the network via gateway."""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    if not gateway_client:
        if config.no_model_mode:
            return {"object": "list", "data": [], "total_models": 0, "total_nodes": 0}
        local_model = OpenAIModel(id=config.model_name, created=int(time.time()), owned_by="llamanet")
        return {"object": "list", "data": [local_model.dict()], "total_models": 1, "total_nodes": 1}

    peers = await gateway_client.get_peers()
    models_dict: Dict[str, Any] = {}

    if not config.no_model_mode:
        metrics = llm.get_metrics() if llm else {}
        models_dict[config.model_name] = {
            "id": config.model_name, "object": "model", "created": int(time.time()),
            "owned_by": "llamanet", "node_count": 0, "nodes": [],
        }
        models_dict[config.model_name]["nodes"].append({
            "node_id": config.node_id, "ip": get_host_ip(), "port": config.port,
            "load": metrics.get("load", 0), "tps": metrics.get("tps", 0),
            "uptime": metrics.get("uptime", 0), "last_seen": int(time.time()),
            "ttft": metrics.get("ttft", 0), "latency": metrics.get("latency", 0),
            "total_tokens": metrics.get("total_tokens", 0),
        })
        models_dict[config.model_name]["node_count"] = 1

    for peer in peers:
        model = peer.get("model", "unknown")
        if model not in models_dict:
            models_dict[model] = {
                "id": model, "object": "model", "created": int(time.time()),
                "owned_by": "llamanet", "node_count": 0, "nodes": [],
            }
        models_dict[model]["node_count"] += 1
        models_dict[model]["nodes"].append(peer)

    return {"object": "list", "data": list(models_dict.values()), "total_models": len(models_dict), "total_nodes": len(peers) + (0 if config.no_model_mode else 1)}

@app.get("/models/statistics")
async def get_models_statistics():
    """Get detailed statistics about models available on the network"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    # In no-model mode with no DHT discovery, return empty stats gracefully
    if config.no_model_mode and not dht_discovery:
        return {
            "network_summary": {
                "total_models": 0,
                "total_nodes": 0,
                "avg_network_load": 0,
                "total_network_tps": 0,
                "timestamp": time.time()
            },
            "models": {}
        }
    
    if not dht_discovery:
        raise HTTPException(status_code=503, detail="DHT discovery not initialized")
    
    try:
        # Get all nodes from the network
        all_nodes = await dht_discovery.get_nodes(force_refresh=True)
        
        # Calculate statistics
        models_dict = {}
        total_load = 0
        total_tps = 0
        
        # Include current node with fresh model info (DHT may have stale data)
        current_node_included = False
        metrics = {}
        if not config.no_model_mode and llm:
            metrics = llm.get_metrics()
            current_node_data = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": config.model_name,
                "load": metrics.get("load", 0.0),
                "tps": metrics.get("tps", 0.0),
                "uptime": metrics.get("uptime", 0),
                "last_seen": int(time.time()),
                "ttft": metrics.get("ttft", 0),
                "latency": metrics.get("latency", 0),
                "total_tokens": metrics.get("total_tokens", 0)
            }
            current_node_included = True
        
        for node in all_nodes:
            # If this node is the current node, override with fresh local data
            if current_node_included and node.node_id == config.node_id:
                model_name = config.model_name
                node_info = {
                    **current_node_data,
                    "ttft": metrics.get("ttft", 0),
                    "latency": metrics.get("latency", 0)
                }
            else:
                model_name = node.model
                node_info = node
            
            if model_name not in models_dict:
                models_dict[model_name] = {
                    "nodes": [],
                    "total_load": 0,
                    "total_tps": 0
                }
            
            models_dict[model_name]["nodes"].append(node_info)
            if isinstance(node_info, dict):
                models_dict[model_name]["total_load"] += node_info.get("load", 0)
                models_dict[model_name]["total_tps"] += node_info.get("tps", 0)
                total_load += node_info.get("load", 0)
                total_tps += node_info.get("tps", 0)
            else:
                models_dict[model_name]["total_load"] += node_info.load
                models_dict[model_name]["total_tps"] += node_info.tps
                total_load += node_info.load
                total_tps += node_info.tps
        
        # Add current node if it wasn't found in DHT results
        if current_node_included and config.model_name not in models_dict:
            models_dict[config.model_name] = {
                "nodes": [current_node_data],
                "total_load": current_node_data["load"],
                "total_tps": current_node_data["tps"]
            }
        
        total_node_count = len(all_nodes) + (1 if current_node_included and not any(n.node_id == config.node_id for n in all_nodes) else 0)
        
        # Format response
        statistics = {
            "network_summary": {
                "total_models": len(models_dict),
                "total_nodes": total_node_count,
                "avg_network_load": total_load / total_node_count if total_node_count > 0 else 0,
                "total_network_tps": total_tps,
                "timestamp": time.time()
            },
            "models": {}
        }
        
        for model_name, model_data in models_dict.items():
            nodes = model_data["nodes"]
            node_count = len(nodes)
            
            # Calculate stats handling both dict and object nodes
            load_values = [n.get("load", 0) if isinstance(n, dict) else n.load for n in nodes]
            tps_values = [n.get("tps", 0) if isinstance(n, dict) else n.tps for n in nodes]
            
            best_node = None
            if nodes:
                best_idx = load_values.index(min(load_values))
                best_n = nodes[best_idx]
                if isinstance(best_n, dict):
                    best_node = best_n
                else:
                    best_node = best_n.__dict__
            
            statistics["models"][model_name] = {
                "node_count": node_count,
                "avg_load": sum(load_values) / node_count if node_count > 0 else 0,
                "total_tps": sum(tps_values),
                "best_node": best_node,
                "availability": "high" if node_count > 2 else "medium" if node_count > 1 else "low",
                "nodes": [
                    {
                        "node_id": n.get("node_id") if isinstance(n, dict) else n.node_id,
                        "ip": n.get("ip") if isinstance(n, dict) else n.ip,
                        "port": n.get("port") if isinstance(n, dict) else n.port,
                        "load": n.get("load", 0) if isinstance(n, dict) else n.load,
                        "tps": n.get("tps", 0) if isinstance(n, dict) else n.tps,
                        "uptime": n.get("uptime", 0) if isinstance(n, dict) else n.uptime,
                        "last_seen": n.get("last_seen") if isinstance(n, dict) else n.last_seen,
                        "ttft": n.get("ttft") if isinstance(n, dict) else getattr(n, 'ttft', None),
                        "latency": n.get("latency") if isinstance(n, dict) else getattr(n, 'latency', None),
                        "total_tokens": n.get("total_tokens") if isinstance(n, dict) else getattr(n, 'total_tokens', None)
                    } for n in nodes
                ]
            }
        
        return statistics
        
    except Exception as e:
        logger.error(f"Error getting model statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/completions")
async def create_completion(request: OpenAICompletionRequest):
    """Text completion — handle locally or route to best peer."""
    if config and config.no_model_mode and not llm:
        if not gateway_client:
            raise HTTPException(status_code=503, detail="No model loaded, no peers available")
        peer = await gateway_client.select_node(
            model=getattr(request, 'model', None),
            strategy=getattr(request, 'strategy', 'load_balanced'),
        )
        if not peer:
            raise HTTPException(status_code=503, detail="No peers available for this model")
        return await _forward_request(request.dict(), "completions", peer, stream=request.stream)

    if not llm or not request_queue_manager:
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
                "model": config.model_name,
                "processing_node": "local",
                "queued": True
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
            "model": config.model_name,
            "processing_node": "local",
            "queued": True
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
            "processing_node": "local"
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
    """Forward completion request to another node using robust SSE handling"""
    try:
        # Remove strategy to prevent infinite forwarding
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        
        url = f"http://{target_node.ip}:{target_node.port}/v1/completions"
        
        if request.stream:
            # Use the new SSE forwarder for streaming
            request_id = f"cmpl-{uuid.uuid4().hex[:8]}"
            sse_forwarder = SSEForwarder(timeout=30)
            
            async def forwarded_stream_generator():
                async for chunk in sse_forwarder.forward_completion_stream(url, request_dict):
                    yield chunk
            
            # Create node info for forwarded streaming
            node_info = {
                "node_id": target_node.node_id,
                "ip": target_node.ip,
                "port": target_node.port,
                "model": target_node.model,
                "processing_node": "forwarded",
                "forwarded_from": config.node_id
            }
            
            return StreamingResponse(
                create_streaming_completion_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=forwarded_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        else:
            # Non-streaming request
            timeout = aiohttp.ClientTimeout(total=30, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json=request_dict,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status == 200:
                        response_data = await response.json()
                        # Add forwarding info to node_info if it exists
                        if "node_info" in response_data and response_data["node_info"]:
                            response_data["node_info"]["processing_node"] = "forwarded"
                            response_data["node_info"]["forwarded_from"] = config.node_id
                        return OpenAICompletionResponse(**response_data)
                    else:
                        error_text = await response.text()
                        logger.error(f"Forwarded completion failed: {response.status} {error_text}")
                        raise HTTPException(status_code=response.status, detail=error_text)
                        
    except asyncio.TimeoutError:
        logger.error(f"Timeout forwarding request to {target_node.node_id[:8]}")
        # Fall back to local processing
        return await _handle_completion_locally(request)
    except Exception as e:
        logger.error(f"Error forwarding request to {target_node.node_id[:8]}: {e}")
        # Fall back to local processing
        return await _handle_completion_locally(request)

@app.post("/v1/chat/completions")
async def create_chat_completion(request: OpenAIChatCompletionRequest):
    """Chat completion — handle locally or route to best peer."""
    if config and config.no_model_mode and not llm:
        if not gateway_client:
            raise HTTPException(status_code=503, detail="No model loaded, no peers available")
        peer = await gateway_client.select_node(
            model=getattr(request, 'model', None),
            strategy=getattr(request, 'strategy', 'load_balanced'),
        )
        if not peer:
            raise HTTPException(status_code=503, detail="No peers available for this model")
        return await _forward_request(request.dict(), "chat/completions", peer, stream=request.stream)

    if not llm or not request_queue_manager:
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
    
    try:
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    # Use the thread-safe streaming method
                    async for chunk in llm.generate_chat_stream_safe(
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
                            "reasoning": chunk.get("reasoning", ""),
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
                "model": config.model_name,
                "processing_node": "local",
                "chat_template": "auto",
                "queued": True,
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
        
        # NON-STREAMING: Use thread-safe method
        result = await llm.generate_chat_safe(
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
            "model": config.model_name,
            "processing_node": "local",
            "chat_template": "auto",
            "queued": True,
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
                            "reasoning": chunk.get("reasoning", ""),
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
            "processing_node": "local",
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
    """Forward chat completion request to another node using robust SSE handling"""
    try:
        # Remove strategy to prevent infinite forwarding
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        
        url = f"http://{target_node.ip}:{target_node.port}/v1/chat/completions"
        
        if request.stream:
            # Use the new SSE forwarder for streaming
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            sse_forwarder = SSEForwarder(timeout=30)
            
            async def forwarded_stream_generator():
                async for chunk in sse_forwarder.forward_chat_stream(url, request_dict):
                    yield chunk
            
            # Create node info for forwarded streaming
            node_info = {
                "node_id": target_node.node_id,
                "ip": target_node.ip,
                "port": target_node.port,
                "model": target_node.model,
                "processing_node": "forwarded",
                "forwarded_from": config.node_id
            }
            
            return StreamingResponse(
                create_streaming_chat_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=forwarded_stream_generator(),
                    node_info=node_info
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "text/plain; charset=utf-8"
                }
            )
        else:
            # Non-streaming request
            timeout = aiohttp.ClientTimeout(total=30, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json=request_dict,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status == 200:
                        response_data = await response.json()
                        # Add forwarding info to node_info if it exists
                        if "node_info" in response_data and response_data["node_info"]:
                            response_data["node_info"]["processing_node"] = "forwarded"
                            response_data["node_info"]["forwarded_from"] = config.node_id
                        return OpenAIChatCompletionResponse(**response_data)
                    else:
                        error_text = await response.text()
                        logger.error(f"Forwarded chat completion failed: {response.status} {error_text}")
                        raise HTTPException(status_code=response.status, detail=error_text)
                        
    except asyncio.TimeoutError:
        logger.error(f"Timeout forwarding chat completion to {target_node.node_id[:8]}")
        # Fall back to local processing
        return await _handle_chat_completion_locally(request)
    except Exception as e:
        logger.error(f"Error forwarding chat completion to {target_node.node_id[:8]}: {e}")
        # Fall back to local processing
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
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=request_body, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise HTTPException(status_code=resp.status, detail=error_text)

                if stream:
                    return StreamingResponse(
                        resp.content.iter_any(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Content-Type": "text/event-stream; charset=utf-8", "X-Accel-Buffering": "no"},
                    )
                else:
                    data = await resp.json()
                    return JSONResponse(content=data)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Peer request timed out")

# Status and utility endpoints
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
    return {"status": "ok", "llm_loaded": llm is not None, "model": config.model_name, "timestamp": time.time(), **metrics}



@app.get("/events/network")
async def network_events():
    if not sse_manager:
        raise HTTPException(status_code=503, detail="SSE not initialized")

    async def event_generator():
        connection_id = f"sse_{uuid.uuid4().hex[:8]}"
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
        except asyncio.CancelledError:
            pass
        finally:
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
    """Hot-reload to a different model"""
    global llm
    
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    try:
        body = await request.json()
        model_path = body.get("model_path")
        
        if not model_path:
            raise HTTPException(status_code=400, detail="model_path is required")
        
        if not os.path.exists(model_path):
            raise HTTPException(status_code=404, detail=f"Model file not found: {model_path}")
        
        # In no-model mode, do full initialization
        if config.no_model_mode:
            global heartbeat_manager, dht_publisher
            
            config.model_path = model_path
            config.model_name = os.path.basename(model_path)
            config.no_model_mode = False
            config.save_active_model(model_path, config.model_name)
            
            logger.info(f"Initializing LLM with selected model: {model_path}")
            llm = LlamaWrapper(config)
            
            # Initialize heartbeat manager (needed for /health and metrics)
            try:
                logger.info("Starting heartbeat manager after model load...")
                heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
                await heartbeat_manager.start()
                if shutdown_handler:
                    shutdown_handler.register_component('heartbeat_manager', heartbeat_manager)
                logger.info("✅ Heartbeat manager started")
            except Exception as e:
                logger.warning(f"Failed to start heartbeat manager: {e}")
            
            # Initialize DHT publisher (advertise new model to network)
            try:
                logger.info("Starting DHT publisher after model load...")
                dht_publisher = DHTPublisher(config, llm.get_metrics)
                await dht_publisher.start()
                if shutdown_handler:
                    shutdown_handler.register_component('dht_publisher', dht_publisher)
                
                # Connect publisher to DHT service for bootstrap events
                from common.dht_service import SharedDHTService
                dht_svc = SharedDHTService()
                if dht_svc.is_initialized():
                    dht_svc.set_event_publisher(dht_publisher)
                    logger.info("✅ Event publisher connected to DHT service")
                
                # Trigger DHT join event so workers see this node
                asyncio.create_task(trigger_post_uvicorn_join())
                logger.info("✅ DHT publisher started and join event scheduled")
            except Exception as e:
                logger.warning(f"Failed to start DHT publisher: {e}")
            
            return {
                "success": True,
                "data": {
                    "model_path": model_path,
                    "model_name": config.model_name,
                    "mode": "initial_load",
                    "reloaded": True
                },
                "message": f"Model loaded: {config.model_name}",
                "timestamp": time.time()
            }
        
        # In normal mode, do hot-reload
        if not llm:
            raise HTTPException(status_code=503, detail="LLM wrapper not initialized")
        
        if model_path == config.model_path:
            return {
                "success": True,
                "data": {
                    "model_path": model_path,
                    "model_name": config.model_name,
                    "mode": "already_loaded",
                    "reloaded": False
                },
                "message": f"Model already loaded: {config.model_name}",
                "timestamp": time.time()
            }
        
        # Pause request queue during reload
        await request_queue_manager.set_reloading(True)
        
        try:
            drained = await request_queue_manager.drain_active_requests(timeout=30.0)
            if not drained:
                logger.warning("Not all requests drained - proceeding with reload anyway")
            
            logger.info(f"Hot-reloading model: {config.model_path} -> {model_path}")
            llm.reload_model(model_path)

            config.save_active_model(model_path, config.model_name)

            # Notify bootstrap peers of model change
            asyncio.create_task(_notify_bootstrap_peers_of_event("node_updated"))
            
            # Re-publish to DHT with updated model info
            if dht_publisher and dht_publisher.running:
                try:
                    await dht_publisher.send_post_uvicorn_join_event()
                    logger.info(f"DHT re-published with new model: {config.model_name}")
                except Exception as e:
                    logger.warning(f"Failed to update DHT after reload: {e}")
            
            # Broadcast node_updated event via SSE so connected UIs see the change
            if sse_manager:
                try:
                    await sse_manager.broadcast_event("node_updated", {
                        "node_info": {
                            "node_id": config.node_id,
                            "ip": get_host_ip(),
                            "port": config.port,
                            "model": config.model_name,
                            "load": 0.0,
                            "tps": 0.0,
                            "uptime": 0,
                            "last_seen": int(time.time()),
                            "dht_port": config.dht_port
                        },
                        "timestamp": time.time(),
                        "source": "model_reload",
                        "event_driven": True
                    })
                    logger.info(f"SSE broadcast: node_updated with new model {config.model_name}")
                except Exception as e:
                    logger.warning(f"Failed to broadcast model change via SSE: {e}")
            
            # Force DHT discovery to refresh its local node cache
            if dht_discovery:
                try:
                    # Get fresh nodes to update discovery cache
                    await dht_discovery.get_nodes(force_refresh=True)
                    logger.info("DHT discovery cache refreshed after model reload")
                except Exception as e:
                    logger.debug(f"DHT discovery refresh not critical: {e}")
            
            return {
                "success": True,
                "data": {
                    "model_path": model_path,
                    "model_name": config.model_name,
                    "mode": "hot_reload",
                    "reloaded": True,
                    "drained": drained
                },
                "message": f"Model hot-reloaded: {config.model_name}",
                "timestamp": time.time()
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
  --dht-port PORT      DHT protocol port (default: 8001)
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
    --dht-port 8003 \\
    --bootstrap-nodes localhost:8001

Environment Variables:
  MODEL_PATH, HOST, PORT, DHT_PORT, NODE_ID, BOOTSTRAP_NODES
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
