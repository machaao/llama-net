import asyncio
import time
import uuid
import uvicorn
import os
import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from typing import Dict, Any, Union, List, Optional
from contextlib import asynccontextmanager
import json

from common.models import (
    OpenAIModel, OpenAIModelList,
    OpenAICompletionRequest, OpenAIChatCompletionRequest,
    OpenAICompletionResponse, OpenAIChatCompletionResponse,
    OpenAIChoice, OpenAIUsage, OpenAIMessage,
    create_streaming_chat_response, create_streaming_completion_response
)
from common.sse_handler import SSEForwarder

from inference_node.config import InferenceConfig
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.metrics import SystemInfo
from inference_node.dht_publisher import DHTPublisher
from inference_node.heartbeat import HeartbeatManager
from inference_node.p2p_handler import P2PRequestHandler
from client.dht_discovery import DHTDiscovery
from client.router import NodeSelector
from common.utils import get_logger, get_host_ip

logger = get_logger(__name__)

# Global variables
config = None
llm = None
dht_publisher = None
system_info = None
heartbeat_manager = None
dht_discovery = None
node_selector = None
p2p_handler = None
round_robin_state = {"index": 0}  # Global round robin state

# Request concurrency limiting
REQUEST_SEMAPHORE = asyncio.Semaphore(10)  # Max 10 concurrent requests

async def cleanup_services():
    """Clean up all services in reverse order"""
    global heartbeat_manager, p2p_handler, dht_publisher, dht_discovery
    
    logger.info("Shutting down services...")
    
    # Stop services in reverse order
    if heartbeat_manager:
        try:
            await heartbeat_manager.stop()
        except Exception as e:
            logger.error(f"Error stopping heartbeat manager: {e}")
    
    if p2p_handler:
        try:
            await p2p_handler.close()
        except Exception as e:
            logger.error(f"Error stopping P2P handler: {e}")
    
    if dht_publisher:
        try:
            await dht_publisher.stop()
        except Exception as e:
            logger.error(f"Error stopping DHT publisher: {e}")
    
    if dht_discovery:
        try:
            await dht_discovery.stop()
        except Exception as e:
            logger.error(f"Error stopping DHT discovery: {e}")
    
    # Stop the shared DHT service last
    try:
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        await dht_service.stop()
    except Exception as e:
        logger.error(f"Error stopping shared DHT service: {e}")
    
    logger.info("All services shut down")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global config, llm, dht_publisher, system_info, heartbeat_manager, dht_discovery, node_selector, p2p_handler
    
    try:
        # Load configuration
        config = InferenceConfig()
        logger.info(f"Starting OpenAI-compatible inference node: {config}")
        
        # Initialize LLM first (most likely to fail)
        logger.info("Initializing LLM...")
        llm = LlamaWrapper(config)
        logger.info("LLM initialized successfully")
        
        # Get system info
        system_info = SystemInfo.get_all_info()
        
        # Start heartbeat manager (lightweight)
        logger.info("Starting heartbeat manager...")
        heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
        await heartbeat_manager.start()
        
        # Initialize shared DHT service FIRST
        logger.info("Initializing shared DHT service...")
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        
        # Parse bootstrap nodes
        bootstrap_nodes = []
        if config.bootstrap_nodes:
            for node_str in config.bootstrap_nodes.split(','):
                try:
                    ip, port = node_str.strip().split(':')
                    bootstrap_nodes.append((ip, int(port)))
                except ValueError:
                    logger.warning(f"Invalid bootstrap node format: {node_str}")
        
        # Initialize the shared DHT service
        await dht_service.initialize(config.node_id, config.dht_port, bootstrap_nodes)
        
        # Start DHT publisher (uses shared service)
        logger.info("Starting DHT publisher...")
        dht_publisher = DHTPublisher(config, llm.get_metrics)
        await dht_publisher.start()
        
        # Start DHT discovery (uses shared service)
        logger.info("Starting DHT discovery...")
        dht_discovery = DHTDiscovery(config.bootstrap_nodes, config.dht_port)
        await dht_discovery.start()
        
        # Initialize node selector
        node_selector = NodeSelector(dht_discovery)
        
        # Start P2P handler LAST (most likely to have conflicts)
        try:
            logger.info("Starting P2P handler...")
            p2p_handler = P2PRequestHandler(config, llm)
            await p2p_handler.start()
            if p2p_handler:
                dht_publisher.p2p_handler = p2p_handler
            logger.info("P2P handler started successfully")
        except Exception as e:
            logger.warning(f"P2P handler failed to start (continuing without P2P): {e}")
            p2p_handler = None
        
        logger.info("🚀 All services started successfully!")
        
    except Exception as e:
        logger.error(f"Failed to start services: {e}")
        # Cleanup any partially started services
        await cleanup_services()
        raise
    
    yield
    
    # Shutdown
    await cleanup_services()

app = FastAPI(title="LlamaNet OpenAI-Compatible Inference Node", lifespan=lifespan)

# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

def _should_forward_request(request, target_node) -> bool:
    """Check if request should be forwarded to avoid loops"""
    # Don't forward if target is ourselves
    if target_node.node_id == config.node_id:
        return False
    
    # Don't forward if strategy is 'local'
    strategy = getattr(request, 'strategy', None)
    if strategy == 'local':
        return False
    
    # Add forwarding hop count to prevent infinite loops
    forwarded_count = getattr(request, '_forwarded_count', 0)
    if forwarded_count >= 2:  # Max 2 hops
        logger.warning(f"Request already forwarded {forwarded_count} times, processing locally")
        return False
    
    return True

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
    """List available models (OpenAI-compatible)"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    return OpenAIModelList(
        data=[
            OpenAIModel(
                id=config.model_name,
                created=int(time.time()),
                owned_by="llamanet"
            )
        ]
    )

@app.get("/v1/models/network")
async def list_network_models():
    """List all available models across the network (OpenAI-compatible extension)"""
    if not dht_discovery:
        raise HTTPException(status_code=503, detail="DHT discovery not initialized")
    
    try:
        # Get all nodes from the network
        all_nodes = await dht_discovery.get_nodes(force_refresh=True)
        
        # Group by model and create OpenAI-compatible response
        models_dict = {}
        for node in all_nodes:
            model_name = node.model
            if model_name not in models_dict:
                models_dict[model_name] = {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "llamanet",
                    "node_count": 0,
                    "nodes": []
                }
            
            models_dict[model_name]["node_count"] += 1
            models_dict[model_name]["nodes"].append({
                "node_id": node.node_id,
                "ip": node.ip,
                "port": node.port,
                "load": node.load,
                "tps": node.tps,
                "last_seen": node.last_seen
            })
        
        return {
            "object": "list",
            "data": list(models_dict.values()),
            "total_models": len(models_dict),
            "total_nodes": len(all_nodes)
        }
        
    except Exception as e:
        logger.error(f"Error listing network models: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/models/statistics")
async def get_models_statistics():
    """Get detailed statistics about models available on the network"""
    if not dht_discovery:
        raise HTTPException(status_code=503, detail="DHT discovery not initialized")
    
    try:
        # Get all nodes from the network
        all_nodes = await dht_discovery.get_nodes(force_refresh=True)
        
        # Calculate statistics
        models_dict = {}
        total_load = 0
        total_tps = 0
        
        for node in all_nodes:
            model_name = node.model
            if model_name not in models_dict:
                models_dict[model_name] = {
                    "nodes": [],
                    "total_load": 0,
                    "total_tps": 0
                }
            
            models_dict[model_name]["nodes"].append(node)
            models_dict[model_name]["total_load"] += node.load
            models_dict[model_name]["total_tps"] += node.tps
            total_load += node.load
            total_tps += node.tps
        
        # Format response
        statistics = {
            "network_summary": {
                "total_models": len(models_dict),
                "total_nodes": len(all_nodes),
                "avg_network_load": total_load / len(all_nodes) if all_nodes else 0,
                "total_network_tps": total_tps,
                "timestamp": time.time()
            },
            "models": {}
        }
        
        for model_name, model_data in models_dict.items():
            nodes = model_data["nodes"]
            statistics["models"][model_name] = {
                "node_count": len(nodes),
                "avg_load": model_data["total_load"] / len(nodes),
                "total_tps": model_data["total_tps"],
                "best_node": min(nodes, key=lambda n: n.load).__dict__ if nodes else None,
                "availability": "high" if len(nodes) > 2 else "medium" if len(nodes) > 1 else "low",
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "ip": n.ip,
                        "port": n.port,
                        "load": n.load,
                        "tps": n.tps,
                        "uptime": n.uptime,
                        "last_seen": n.last_seen
                    } for n in nodes
                ]
            }
        
        return statistics
        
    except Exception as e:
        logger.error(f"Error getting model statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/completions")
async def create_completion(request: OpenAICompletionRequest):
    """Create a completion (OpenAI-compatible)"""
    async with REQUEST_SEMAPHORE:
        if not llm:
            raise HTTPException(status_code=503, detail="LLM not initialized")
        
        # Check if request has strategy parameter for routing
        strategy = getattr(request, 'strategy', None)
        target_model = getattr(request, 'target_model', None)
        
        # If strategy is specified and we have node selector, try to route request
        if strategy and node_selector and strategy != "local":
            try:
                # Get available nodes
                nodes = await dht_discovery.get_nodes(model=target_model)
                if len(nodes) > 1:  # Only route if there are other nodes
                    selected_node = await node_selector.select_node(
                        model=request.model,
                        strategy=strategy,
                        target_model=target_model  # Pass target model
                    )
                    
                    # Check if we should forward this request
                    if selected_node and _should_forward_request(request, selected_node):
                        # Increment forwarding count
                        request._forwarded_count = getattr(request, '_forwarded_count', 0) + 1
                        logger.info(f"🔄 Routing completion to node {selected_node.node_id[:8]}... (model: {selected_node.model}) via {strategy}")
                        return await _forward_completion(request, selected_node)
            except Exception as e:
                logger.warning(f"Failed to route request, handling locally: {e}")
        
        # Check if we should handle locally based on target model
        if target_model and target_model != config.model_name:
            logger.warning(f"Target model {target_model} requested but this node runs {config.model_name}")
            # Try to find a node with the target model
            try:
                nodes = await dht_discovery.get_nodes(model=target_model)
                if nodes:
                    selected_node = nodes[0]  # Use first available node with target model
                    logger.info(f"🔄 Forwarding to node with target model {target_model}")
                    return await _forward_completion(request, selected_node)
            except Exception as e:
                logger.warning(f"Failed to find node with target model {target_model}: {e}")
        
        # Handle locally (original logic)
        return await _handle_completion_locally(request)

async def _handle_completion_locally(request: OpenAICompletionRequest):
    """Handle completion on this node with robust SSE handling"""
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
                    async for chunk in llm.generate_stream_async(
                        prompt=prompt,
                        max_tokens=request.max_tokens or 100,
                        temperature=request.temperature or 0.7,
                        top_p=request.top_p or 0.9,
                        stop=stop_tokens,
                        repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                    ):
                        # Transform to consistent format
                        yield {
                            "text": chunk.get("text", ""),
                            "finished": chunk.get("finished", False)
                        }
                except asyncio.CancelledError:
                    logger.info("Local streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in local streaming: {e}")
                    # Don't re-raise, just end the stream gracefully
            
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
        
        # Non-streaming response
        result = llm.generate(
            prompt=prompt,
            max_tokens=request.max_tokens or 100,
            temperature=request.temperature or 0.7,
            top_p=request.top_p or 0.9,
            stop=stop_tokens,
            repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
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
            # Non-streaming request (unchanged)
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
    """Create a chat completion (OpenAI-compatible)"""
    async with REQUEST_SEMAPHORE:
        if not llm:
            raise HTTPException(status_code=503, detail="LLM not initialized")
        
        # Check if request has strategy parameter for routing
        strategy = getattr(request, 'strategy', None)
        target_model = getattr(request, 'target_model', None)
        
        # If strategy is specified and we have node selector, try to route request
        if strategy and node_selector and strategy != "local":
            try:
                # Get available nodes
                nodes = await dht_discovery.get_nodes(model=target_model)
                if len(nodes) > 1:  # Only route if there are other nodes
                    selected_node = await node_selector.select_node(
                        model=request.model,
                        strategy=strategy,
                        target_model=target_model  # Pass target model
                    )
                    
                    # Check if we should forward this request
                    if selected_node and _should_forward_request(request, selected_node):
                        # Increment forwarding count
                        request._forwarded_count = getattr(request, '_forwarded_count', 0) + 1
                        logger.info(f"🔄 Routing chat completion to node {selected_node.node_id[:8]}... (model: {selected_node.model}) via {strategy}")
                        return await _forward_chat_completion(request, selected_node)
            except Exception as e:
                logger.warning(f"Failed to route request, handling locally: {e}")
        
        # Check if we should handle locally based on target model
        if target_model and target_model != config.model_name:
            logger.warning(f"Target model {target_model} requested but this node runs {config.model_name}")
            # Try to find a node with the target model
            try:
                nodes = await dht_discovery.get_nodes(model=target_model)
                if nodes:
                    selected_node = nodes[0]  # Use first available node with target model
                    logger.info(f"🔄 Forwarding to node with target model {target_model}")
                    return await _forward_chat_completion(request, selected_node)
            except Exception as e:
                logger.warning(f"Failed to find node with target model {target_model}: {e}")
        
        # Handle locally (original logic)
        return await _handle_chat_completion_locally(request)

async def _handle_chat_completion_locally(request: OpenAIChatCompletionRequest):
    """Handle chat completion on this node with robust SSE handling"""
    # Convert messages to a single prompt - IMPROVED FORMAT
    prompt_parts = []
    for message in request.messages:
        if message.role == "system":
            prompt_parts.append(f"System: {message.content}")
        elif message.role == "user":
            prompt_parts.append(f"Human: {message.content}")
        elif message.role == "assistant":
            prompt_parts.append(f"Assistant: {message.content}")
    
    # Use a more standard chat format
    prompt = "\n\n".join(prompt_parts) + "\n\nAssistant:"
    
    # IMPROVED: Add proper stop tokens for chat format
    stop_tokens = None
    if request.stop:
        if isinstance(request.stop, str):
            stop_tokens = [request.stop] if request.stop.strip() else None
        elif isinstance(request.stop, list):
            stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
            stop_tokens = stop_tokens if stop_tokens else None
    else:
        # Default stop tokens for chat format to prevent bleeding
        stop_tokens = ["\n\nHuman:", "\n\nUser:", "\nHuman:", "\nUser:", "Human:", "User:"]
    
    try:
        # Handle streaming with robust error handling
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def local_stream_generator():
                try:
                    async for chunk in llm.generate_stream_async(
                        prompt=prompt,
                        max_tokens=request.max_tokens or 100,
                        temperature=request.temperature or 0.7,
                        top_p=request.top_p or 0.9,
                        stop=stop_tokens,
                        repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                    ):
                        # Transform to consistent format
                        yield {
                            "text": chunk.get("text", ""),
                            "finished": chunk.get("finished", False)
                        }
                except asyncio.CancelledError:
                    logger.info("Local chat streaming cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error in local chat streaming: {e}")
                    # Don't re-raise, just end the stream gracefully
            
            # Create node info for streaming
            node_info = {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "model": config.model_name,
                "processing_node": "local"
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
        
        # Non-streaming response
        result = llm.generate(
            prompt=prompt,
            max_tokens=request.max_tokens or 100,
            temperature=request.temperature or 0.7,
            top_p=request.top_p or 0.9,
            stop=stop_tokens,
            repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
        )
        
        # Calculate token counts (approximate)
        prompt_tokens = len(prompt.split())
        completion_tokens = result["tokens_generated"]
        
        # Create response message
        response_message = OpenAIMessage(
            role="assistant",
            content=result["text"].strip()
        )
        
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
            "processing_node": "local"
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
            # Non-streaming request (unchanged)
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

# Status and utility endpoints
@app.get("/status")
async def status():
    """Get node status"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
        
    return llm.get_metrics()

@app.get("/info")
async def info():
    """Get static node information"""
    if not config or not system_info:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    # Get P2P info if available
    p2p_info = {}
    if p2p_handler:
        p2p_info = p2p_handler.get_p2p_info()
    
    # Get hardware fingerprint info
    hardware_info = config.get_hardware_info()
        
    return {
        "node_id": config.node_id,
        "model": config.model_name,
        "model_path": config.model_path,
        "system": system_info,
        "dht_port": config.dht_port,
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/completions", "/v1/chat/completions"],
        "p2p_enabled": bool(p2p_handler),
        "hardware_based_id": True,
        "hardware_fingerprint": hardware_info,
        **p2p_info
    }

@app.get("/health")
async def health():
    """Get node health status"""
    if not heartbeat_manager:
        raise HTTPException(status_code=503, detail="Heartbeat manager not initialized")
        
    health_status = heartbeat_manager.get_health_status()
    
    # Add additional health checks
    health_status.update({
        "llm_loaded": llm is not None,
        "dht_running": dht_publisher is not None and dht_publisher.running,
        "timestamp": time.time(),
        "openai_compatible": True
    })
    
    return health_status

@app.get("/nodes")
async def get_all_nodes():
    """Get all discovered nodes with enhanced discovery"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
    
    try:
        # Get published nodes from multiple sources
        all_published_nodes = []
        
        # 1. Get from all_nodes key
        all_nodes_data = await dht_publisher.kademlia_node.find_value("all_nodes")
        if all_nodes_data:
            if isinstance(all_nodes_data, list):
                all_published_nodes.extend(all_nodes_data)
            else:
                all_published_nodes.append(all_nodes_data)
        
        # 2. Get from model-specific keys
        try:
            model_data = await dht_publisher.kademlia_node.find_value(f"model:{config.model_name}")
            if model_data:
                if isinstance(model_data, list):
                    all_published_nodes.extend(model_data)
                else:
                    all_published_nodes.append(model_data)
        except Exception as e:
            logger.debug(f"No model-specific data found: {e}")
        
        # Process published nodes only
        current_time = time.time()
        published_nodes = []
        
        # Deduplicate published nodes by node_id
        seen_node_ids = set()
        for node_data in all_published_nodes:
            if isinstance(node_data, dict) and node_data.get('node_id'):
                node_id = node_data['node_id']
                last_seen = node_data.get('last_seen', 0)
                
                if node_id not in seen_node_ids and current_time - last_seen < 120:  # 2 minute window
                    published_nodes.append({
                        **node_data,
                        "source": "published"
                    })
                    seen_node_ids.add(node_id)
        
        # Get routing table contacts separately (for debugging/admin purposes)
        routing_contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
        dht_contacts = []
        
        for contact in routing_contacts:
            if contact.node_id not in seen_node_ids and current_time - contact.last_seen < 60:
                # Try to get HTTP info
                http_port, model = await _probe_node_info(contact.ip, contact.node_id)
                
                dht_contacts.append({
                    "node_id": contact.node_id,
                    "ip": contact.ip,
                    "port": http_port,
                    "model": model,
                    "load": 0.0,
                    "tps": 0.0,
                    "uptime": 0,
                    "last_seen": int(contact.last_seen),
                    "source": "dht_contact"
                })
        
        return {
            "published_nodes": published_nodes,
            "dht_contacts": dht_contacts,
            "total_published": len(published_nodes),
            "total_dht_contacts": len(dht_contacts),
            "sources": {
                "published": len(published_nodes),
                "dht_contacts": len(dht_contacts)
            },
            "discovery_methods": {
                "all_nodes_key": len([n for n in all_published_nodes if isinstance(n, dict)]),
                "routing_table": len(routing_contacts)
            },
            "timestamp": current_time
        }
        
    except Exception as e:
        logger.error(f"Error getting nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _probe_node_info(ip: str, node_id: str) -> tuple:
    """Probe a node to get HTTP port and model info"""
    import aiohttp
    
    for test_port in [8000, 8002, 8004, 8006]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                async with session.get(f"http://{ip}:{test_port}/info") as response:
                    if response.status == 200:
                        info = await response.json()
                        return test_port, info.get('model', 'unknown')
        except:
            continue
    
    return 8000, "unknown"  # Default fallback

@app.get("/dht/status")
async def dht_status():
    """Get DHT network status"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
        
    kademlia_node = dht_publisher.kademlia_node
    routing_table = kademlia_node.routing_table
    
    # Get all unique contacts from routing table
    all_contacts = routing_table.get_unique_contacts()
    
    # Format contact information
    contacts = []
    current_time = time.time()
    for contact in all_contacts:
        contacts.append({
            "node_id": contact.node_id,
            "ip": contact.ip,
            "port": contact.port,
            "last_seen": contact.last_seen,
            "seconds_ago": int(current_time - contact.last_seen),
            "status": "active" if current_time - contact.last_seen < 30 else "stale"
        })
    
    # Get cleanup stats
    cleanup_stats = kademlia_node.get_cleanup_stats()
    
    return {
        "node_id": kademlia_node.node_id,
        "dht_port": kademlia_node.port,
        "running": kademlia_node.running,
        "contacts_count": len(all_contacts),
        "contacts": contacts,
        "storage_keys": list(kademlia_node.storage.keys()),
        "bootstrap_nodes": dht_publisher.bootstrap_nodes,
        "cleanup_stats": cleanup_stats
    }

@app.get("/dht/verify")
async def verify_dht_network():
    """Verify DHT network connectivity and data propagation"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
    
    verification_results = {
        "local_storage": {},
        "network_lookup": {},
        "routing_table": {},
        "connectivity": {}
    }
    
    try:
        # Check local storage
        verification_results["local_storage"] = {
            "keys": list(dht_publisher.kademlia_node.storage.keys()),
            "all_nodes_local": "all_nodes" in dht_publisher.kademlia_node.storage
        }
        
        # Check network lookup
        all_nodes_network = await dht_publisher.kademlia_node.find_value("all_nodes")
        verification_results["network_lookup"] = {
            "all_nodes_found": all_nodes_network is not None,
            "all_nodes_type": type(all_nodes_network).__name__,
            "all_nodes_count": len(all_nodes_network) if isinstance(all_nodes_network, list) else (1 if all_nodes_network else 0)
        }
        
        # Check routing table
        contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
        verification_results["routing_table"] = {
            "contact_count": len(contacts),
            "contacts": [{"id": c.node_id[:8], "ip": c.ip, "port": c.port} for c in contacts[:5]]
        }
        
        # Test connectivity to other nodes
        connectivity_tests = []
        for contact in contacts[:3]:  # Test first 3 contacts
            try:
                ping_result = await dht_publisher.kademlia_node._ping_node(contact.ip, contact.port)
                connectivity_tests.append({
                    "node_id": contact.node_id[:8],
                    "address": f"{contact.ip}:{contact.port}",
                    "reachable": ping_result is not None
                })
            except Exception as e:
                connectivity_tests.append({
                    "node_id": contact.node_id[:8],
                    "address": f"{contact.ip}:{contact.port}",
                    "reachable": False,
                    "error": str(e)
                })
        
        verification_results["connectivity"] = connectivity_tests
        
        return verification_results
        
    except Exception as e:
        logger.error(f"DHT verification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/node/{node_id}")
async def get_node_info(node_id: str):
    """Get detailed information about a specific node"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
    
    try:
        # Check if this is the current node
        if node_id == config.node_id:
            # Return current node info with full details
            metrics = llm.get_metrics() if llm else {}
            return {
                "node_id": config.node_id,
                "ip": get_host_ip(),
                "port": config.port,
                "dht_port": config.dht_port,
                "model": config.model_name,
                "model_path": config.model_path,
                "load": metrics.get("load", 0.0),
                "tps": metrics.get("tps", 0.0),
                "uptime": metrics.get("uptime", 0),
                "total_tokens": metrics.get("total_tokens", 0),
                "last_seen": int(time.time()),
                "system": system_info,
                "status": "online",
                "is_current_node": True,
                "openai_compatible": True,
                "endpoints": ["/v1/models", "/v1/completions", "/v1/chat/completions"]
            }
        
        # Look for the node in published data
        all_nodes_data = await dht_publisher.kademlia_node.find_value("all_nodes")
        if all_nodes_data:
            if isinstance(all_nodes_data, list):
                for node_data in all_nodes_data:
                    if isinstance(node_data, dict) and node_data.get('node_id') == node_id:
                        # Try to get additional info from the node directly
                        additional_info = await _get_remote_node_info(node_data.get('ip'), node_data.get('port'))
                        if additional_info:
                            node_data.update(additional_info)
                        
                        node_data["is_current_node"] = False
                        node_data["status"] = "online" if time.time() - node_data.get('last_seen', 0) < 60 else "stale"
                        return node_data
        
        # Look in routing table contacts
        contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
        for contact in contacts:
            if contact.node_id == node_id:
                # Try to get HTTP info
                http_port, model = await _probe_node_info(contact.ip, contact.node_id)
                additional_info = await _get_remote_node_info(contact.ip, http_port)
                
                node_info = {
                    "node_id": contact.node_id,
                    "ip": contact.ip,
                    "port": http_port,
                    "dht_port": contact.port,
                    "model": model,
                    "load": 0.0,
                    "tps": 0.0,
                    "uptime": 0,
                    "last_seen": int(contact.last_seen),
                    "status": "online" if time.time() - contact.last_seen < 60 else "stale",
                    "is_current_node": False,
                    "source": "dht_contact"
                }
                
                if additional_info:
                    node_info.update(additional_info)
                
                return node_info
        
        # Node not found
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting node info for {node_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _get_remote_node_info(ip: str, port: int) -> Optional[Dict[str, Any]]:
    """Get additional information from a remote node"""
    if not ip or not port:
        return None
    
    import aiohttp
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            # Get basic info
            async with session.get(f"http://{ip}:{port}/info") as response:
                if response.status == 200:
                    info = await response.json()
                    
                    # Get status/metrics
                    try:
                        async with session.get(f"http://{ip}:{port}/status") as status_response:
                            if status_response.status == 200:
                                status_data = await status_response.json()
                                info.update(status_data)
                    except:
                        pass
                    
                    return info
    except Exception as e:
        logger.debug(f"Failed to get remote node info from {ip}:{port}: {e}")
        return None

@app.get("/p2p/status")
async def p2p_status():
    """Get P2P transport status"""
    if not p2p_handler:
        raise HTTPException(status_code=503, detail="P2P handler not initialized")
    
    p2p_info = p2p_handler.get_p2p_info()
    
    return {
        "p2p_enabled": True,
        "transport_running": p2p_handler.transport.running,
        **p2p_info,
        "timestamp": time.time()
    }

@app.get("/events/network")
async def network_events():
    """Server-Sent Events for real-time network updates"""
    async def event_generator():
        try:
            # Create a client-side event listener for the UI
            from client.event_discovery import EventBasedDHTDiscovery, NodeEventListener, NodeEvent
            
            class UIEventListener(NodeEventListener):
                def __init__(self, queue):
                    self.queue = queue
                
                async def on_node_event(self, event: NodeEvent):
                    event_data = {
                        "type": event.event_type.value,
                        "timestamp": event.timestamp,
                        "node_info": event.node_info.dict() if event.node_info else None,
                        "metadata": event.metadata or {}
                    }
                    await self.queue.put(event_data)
            
            # Create event queue for this SSE connection
            event_queue = asyncio.Queue()
            
            # Use the existing DHT discovery but add our listener
            if dht_discovery:
                ui_listener = UIEventListener(event_queue)
                dht_discovery.add_event_listener(ui_listener)
                
                try:
                    # Send initial connection event
                    yield "data: {\"type\": \"connected\", \"timestamp\": " + str(time.time()) + "}\n\n"
                    
                    # Send current nodes
                    current_nodes = await dht_discovery.get_nodes()
                    for node in current_nodes:
                        initial_event = {
                            "type": "node_joined",
                            "timestamp": time.time(),
                            "node_info": node.dict()
                        }
                        yield f"data: {json.dumps(initial_event)}\n\n"
                    
                    # Listen for new events
                    while True:
                        try:
                            # Wait for events with timeout for heartbeat
                            event_data = await asyncio.wait_for(event_queue.get(), timeout=30.0)
                            yield f"data: {json.dumps(event_data)}\n\n"
                        except asyncio.TimeoutError:
                            # Send heartbeat
                            heartbeat = {"type": "heartbeat", "timestamp": time.time()}
                            yield f"data: {json.dumps(heartbeat)}\n\n"
                            
                except asyncio.CancelledError:
                    logger.info("SSE connection cancelled")
                    raise
                finally:
                    # Clean up listener
                    dht_discovery.remove_event_listener(ui_listener)
            else:
                yield "data: {\"error\": \"DHT discovery not available\"}\n\n"
                
        except Exception as e:
            logger.error(f"Error in network events SSE: {e}")
            error_event = {"type": "error", "message": str(e), "timestamp": time.time()}
            yield f"data: {json.dumps(error_event)}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Headers": "Cache-Control"
        }
    )

@app.get("/debug/routing")
async def debug_routing():
    """Debug endpoint to show routing information"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
    
    debug_info = {
        "local_node": {
            "node_id": config.node_id,
            "http_port": config.port,
            "dht_port": config.dht_port,
            "ip": get_host_ip()
        },
        "routing_table": {},
        "storage": {},
        "discovery_test": {}
    }
    
    # Get routing table info
    contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
    debug_info["routing_table"] = {
        "contact_count": len(contacts),
        "contacts": [
            {
                "node_id": c.node_id,
                "ip": c.ip,
                "dht_port": c.port,
                "last_seen": c.last_seen,
                "seconds_ago": int(time.time() - c.last_seen)
            }
            for c in contacts
        ]
    }
    
    # Get storage info
    debug_info["storage"] = {
        "keys": list(dht_publisher.kademlia_node.storage.keys()),
        "entries": {
            key: value for key, value in dht_publisher.kademlia_node.storage.items()
        }
    }
    
    # Test discovery
    try:
        if dht_discovery:
            discovered_nodes = await dht_discovery.get_nodes(force_refresh=True)
            debug_info["discovery_test"] = {
                "discovered_count": len(discovered_nodes),
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "ip": n.ip,
                        "http_port": n.port,
                        "model": n.model,
                        "last_seen": n.last_seen
                    }
                    for n in discovered_nodes
                ]
            }
    except Exception as e:
        debug_info["discovery_test"] = {"error": str(e)}
    
    return debug_info

@app.get("/config")
async def get_configuration():
    """Get current node configuration including hardware fingerprint details"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    try:
        config_summary = config.get_configuration_summary()
        hardware_info = config.get_hardware_info()
        
        return {
            "configuration": config_summary,
            "hardware_fingerprint": hardware_info,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Error getting configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/hardware")
async def hardware_info():
    """Get detailed hardware fingerprint information"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    try:
        from common.hardware_fingerprint import HardwareFingerprint
        fingerprint = HardwareFingerprint()
        
        return {
            "node_id": config.node_id,
            "hardware_based": True,
            "fingerprint_summary": fingerprint.get_fingerprint_summary(),
            "consistency_check": fingerprint.validate_consistency(config.node_id, config.port),
            "generated_node_id": fingerprint.generate_node_id(config.port),
            "stored_node_id": config._get_stored_node_id() if hasattr(config, '_get_stored_node_id') else None,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Error getting hardware info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/hardware/validate")
async def validate_hardware_consistency():
    """Validate hardware consistency and node ID"""
    if not config:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    try:
        from common.hardware_fingerprint import HardwareFingerprint
        fingerprint = HardwareFingerprint()
        
        # Perform validation
        expected_node_id = fingerprint.generate_node_id(config.port)
        is_consistent = config.node_id == expected_node_id
        stored_node_id = config._get_stored_node_id() if hasattr(config, '_get_stored_node_id') else None
        
        validation_result = {
            "current_node_id": config.node_id,
            "expected_node_id": expected_node_id,
            "stored_node_id": stored_node_id,
            "is_consistent": is_consistent,
            "hardware_fingerprint": fingerprint.get_fingerprint_summary(),
            "validation_timestamp": time.time()
        }
        
        # Add recommendations
        if not is_consistent:
            validation_result["recommendations"] = []
            
            if stored_node_id == expected_node_id:
                validation_result["recommendations"].append("Configuration should use stored node ID")
            elif stored_node_id and stored_node_id != expected_node_id:
                validation_result["recommendations"].append("Hardware appears to have changed significantly")
                validation_result["recommendations"].append("Consider updating stored node ID")
            else:
                validation_result["recommendations"].append("Store current hardware-based node ID")
        
        return validation_result
        
    except Exception as e:
        logger.error(f"Error validating hardware consistency: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/hardware/update")
async def update_hardware_node_id():
    """Force update node ID based on current hardware"""
    if not config or not dht_publisher:
        raise HTTPException(status_code=503, detail="Node not initialized")
    
    try:
        # Force hardware revalidation
        if hasattr(dht_publisher, 'force_hardware_revalidation'):
            success = await dht_publisher.force_hardware_revalidation()
            
            if success:
                return {
                    "success": True,
                    "message": "Hardware revalidation completed",
                    "new_node_id": config.node_id,
                    "timestamp": time.time()
                }
            else:
                raise HTTPException(status_code=500, detail="Hardware revalidation failed")
        else:
            raise HTTPException(status_code=501, detail="Hardware revalidation not supported")
            
    except Exception as e:
        logger.error(f"Error updating hardware node ID: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def start_server():
    """Start the inference server"""
    global config
    if config is None:
        config = InferenceConfig()  # Will parse command line args
    
    uvicorn.run(
        "inference_node.server:app",
        host=config.host,
        port=config.port,
        log_level="info"
    )

def show_help():
    """Show help information"""
    print("""
LlamaNet OpenAI-Compatible Inference Node

Usage:
  python -m inference_node.server [OPTIONS]

Options:
  --model-path PATH     Path to the GGUF model file (required)
  --host HOST          Host to bind the service (default: 0.0.0.0)
  --port PORT          HTTP API port (default: 8000)
  --dht-port PORT      DHT protocol port (default: 8001)
  --node-id ID         Unique node identifier (default: auto-generated)
  --bootstrap-nodes    Comma-separated bootstrap nodes (ip:port)

Examples:
  # Start bootstrap node
  python -m inference_node.server --model-path ./models/model.gguf

  # Start additional node
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
