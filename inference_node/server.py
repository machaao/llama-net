import asyncio
import time
import uuid
import uvicorn
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from typing import Dict, Any, Union, List
from contextlib import asynccontextmanager
import json

from common.models import (
    OpenAIModel, OpenAIModelList,
    OpenAICompletionRequest, OpenAIChatCompletionRequest,
    OpenAICompletionResponse, OpenAIChatCompletionResponse,
    OpenAIChoice, OpenAIUsage, OpenAIMessage,
    create_streaming_chat_response, create_streaming_completion_response
)

from inference_node.config import InferenceConfig
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.metrics import SystemInfo
from inference_node.dht_publisher import DHTPublisher
from inference_node.heartbeat import HeartbeatManager
from client.dht_discovery import DHTDiscovery
from client.router import NodeSelector
from common.utils import get_logger

logger = get_logger(__name__)

# Global variables
config = None
llm = None
dht_publisher = None
system_info = None
heartbeat_manager = None
dht_discovery = None
node_selector = None
round_robin_state = {"index": 0}  # Global round robin state

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global config, llm, dht_publisher, system_info, heartbeat_manager, dht_discovery, node_selector
    
    # Load configuration
    config = InferenceConfig()
    logger.info(f"Starting OpenAI-compatible inference node: {config}")
    
    # Initialize LLM
    llm = LlamaWrapper(config)
    
    # Get system info
    system_info = SystemInfo.get_all_info()
    
    # Start heartbeat manager
    heartbeat_manager = HeartbeatManager(config.node_id, llm.get_metrics)
    await heartbeat_manager.start()
    
    # Start DHT publisher
    dht_publisher = DHTPublisher(config, llm.get_metrics)
    await dht_publisher.start()
    
    # Initialize DHT discovery for routing (use different port to avoid conflicts)
    dht_discovery = DHTDiscovery(config.bootstrap_nodes, config.dht_port + 100)
    await dht_discovery.start()
    
    # Initialize node selector for request routing
    node_selector = NodeSelector(dht_discovery)
    
    yield
    
    # Shutdown
    if heartbeat_manager:
        await heartbeat_manager.stop()
    if dht_publisher:
        await dht_publisher.stop()
    if dht_discovery:
        await dht_discovery.stop()

app = FastAPI(title="LlamaNet OpenAI-Compatible Inference Node", lifespan=lifespan)

# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

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

@app.post("/v1/completions")
async def create_completion(request: OpenAICompletionRequest):
    """Create a completion (OpenAI-compatible)"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
    
    # Check if request has strategy parameter for routing
    strategy = getattr(request, 'strategy', None)
    
    # If strategy is specified and we have node selector, try to route request
    if strategy and node_selector and strategy != "local":
        try:
            # Get available nodes
            nodes = await dht_discovery.get_nodes()
            if len(nodes) > 1:  # Only route if there are other nodes
                selected_node = await node_selector.select_node(
                    model=request.model,
                    strategy=strategy
                )
                
                # If selected node is not us, forward the request
                if selected_node and selected_node.node_id != config.node_id:
                    logger.info(f"🔄 Routing completion to node {selected_node.node_id[:8]}... via {strategy}")
                    return await _forward_completion(request, selected_node)
        except Exception as e:
            logger.warning(f"Failed to route request, handling locally: {e}")
    
    # Handle locally (original logic)
    return await _handle_completion_locally(request)

async def _handle_completion_locally(request: OpenAICompletionRequest):
    """Handle completion on this node"""
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
        # Handle streaming
        if request.stream:
            request_id = f"cmpl-{uuid.uuid4().hex[:8]}"
            
            async def stream_generator():
                async for chunk in llm.generate_stream_async(
                    prompt=prompt,
                    max_tokens=request.max_tokens or 100,
                    temperature=request.temperature or 0.7,
                    top_p=request.top_p or 0.9,
                    stop=stop_tokens,
                    repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                ):
                    yield chunk
            
            return StreamingResponse(
                create_streaming_completion_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=stream_generator()
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
        
        return OpenAICompletionResponse(
            id=f"cmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[choice],
            usage=usage
        )
        
    except Exception as e:
        logger.error(f"Completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _forward_completion(request: OpenAICompletionRequest, target_node):
    """Forward completion request to another node"""
    import aiohttp
    
    try:
        url = f"http://{target_node.ip}:{target_node.port}/v1/completions"
        
        # Remove strategy to prevent infinite forwarding
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=request_dict,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status == 200:
                    response_data = await response.json()
                    return OpenAICompletionResponse(**response_data)
                else:
                    error_text = await response.text()
                    logger.error(f"Forwarded request failed: {response.status} {error_text}")
                    raise HTTPException(status_code=response.status, detail=error_text)
                    
    except Exception as e:
        logger.error(f"Error forwarding request to {target_node.node_id[:8]}: {e}")
        # Fall back to local processing
        return await _handle_completion_locally(request)

@app.post("/v1/chat/completions")
async def create_chat_completion(request: OpenAIChatCompletionRequest):
    """Create a chat completion (OpenAI-compatible)"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
    
    # Check if request has strategy parameter for routing
    strategy = getattr(request, 'strategy', None)
    
    # If strategy is specified and we have node selector, try to route request
    if strategy and node_selector and strategy != "local":
        try:
            # Get available nodes
            nodes = await dht_discovery.get_nodes()
            if len(nodes) > 1:  # Only route if there are other nodes
                selected_node = await node_selector.select_node(
                    model=request.model,
                    strategy=strategy
                )
                
                # If selected node is not us, forward the request
                if selected_node and selected_node.node_id != config.node_id:
                    logger.info(f"🔄 Routing chat completion to node {selected_node.node_id[:8]}... via {strategy}")
                    return await _forward_chat_completion(request, selected_node)
        except Exception as e:
            logger.warning(f"Failed to route request, handling locally: {e}")
    
    # Handle locally (original logic)
    return await _handle_chat_completion_locally(request)

async def _handle_chat_completion_locally(request: OpenAIChatCompletionRequest):
    """Handle chat completion on this node"""
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
        # Handle streaming with FIXED response format
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def stream_generator():
                async for chunk in llm.generate_stream_async(
                    prompt=prompt,
                    max_tokens=request.max_tokens or 100,
                    temperature=request.temperature or 0.7,
                    top_p=request.top_p or 0.9,
                    stop=stop_tokens,
                    repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
                ):
                    yield chunk
            
            return StreamingResponse(
                create_streaming_chat_response(
                    request_id=request_id,
                    model=request.model,
                    stream_generator=stream_generator()
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
        
        return OpenAIChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=request.model,
            choices=[choice],
            usage=usage
        )
        
    except Exception as e:
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def _forward_chat_completion(request: OpenAIChatCompletionRequest, target_node):
    """Forward chat completion request to another node"""
    import aiohttp
    
    try:
        url = f"http://{target_node.ip}:{target_node.port}/v1/chat/completions"
        
        # Remove strategy to prevent infinite forwarding
        request_dict = request.dict()
        request_dict.pop('strategy', None)
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=request_dict,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status == 200:
                    response_data = await response.json()
                    return OpenAIChatCompletionResponse(**response_data)
                else:
                    error_text = await response.text()
                    logger.error(f"Forwarded request failed: {response.status} {error_text}")
                    raise HTTPException(status_code=response.status, detail=error_text)
                    
    except Exception as e:
        logger.error(f"Error forwarding request to {target_node.node_id[:8]}: {e}")
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
        
    return {
        "node_id": config.node_id,
        "model": config.model_name,
        "model_path": config.model_path,
        "system": system_info,
        "dht_port": config.dht_port,
        "openai_compatible": True,
        "endpoints": ["/v1/models", "/v1/completions", "/v1/chat/completions"]
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
        
        # 3. Get routing table contacts
        routing_contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
        
        # Deduplicate and process
        current_time = time.time()
        unique_nodes = {}
        
        # Process published nodes first (higher priority)
        for node_data in all_published_nodes:
            if isinstance(node_data, dict) and node_data.get('node_id'):
                node_id = node_data['node_id']
                last_seen = node_data.get('last_seen', 0)
                
                if current_time - last_seen < 120:  # 2 minute window for published data
                    unique_nodes[node_id] = {
                        **node_data,
                        "source": "published"
                    }
        
        # Add routing contacts that aren't already present
        for contact in routing_contacts:
            if contact.node_id not in unique_nodes and current_time - contact.last_seen < 60:
                # Try to get HTTP info
                http_port, model = await _probe_node_info(contact.ip, contact.node_id)
                
                unique_nodes[contact.node_id] = {
                    "node_id": contact.node_id,
                    "ip": contact.ip,
                    "port": http_port,
                    "model": model,
                    "load": 0.0,
                    "tps": 0.0,
                    "uptime": 0,
                    "last_seen": int(contact.last_seen),
                    "source": "dht_contact"
                }
        
        active_nodes = list(unique_nodes.values())
        
        return {
            "nodes": active_nodes,
            "total_count": len(active_nodes),
            "sources": {
                "published": len([n for n in active_nodes if n.get("source") == "published"]),
                "dht_contacts": len([n for n in active_nodes if n.get("source") == "dht_contact"])
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
