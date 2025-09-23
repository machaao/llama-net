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
from common.utils import get_logger

logger = get_logger(__name__)

# Global variables
config = None
llm = None
dht_publisher = None
system_info = None
heartbeat_manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global config, llm, dht_publisher, system_info, heartbeat_manager
    
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
    
    yield
    
    # Shutdown
    if heartbeat_manager:
        await heartbeat_manager.stop()
    if dht_publisher:
        await dht_publisher.stop()

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

@app.post("/v1/chat/completions")
async def create_chat_completion(request: OpenAIChatCompletionRequest):
    """Create a chat completion (OpenAI-compatible)"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
    
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
    """Get all discovered nodes with their models"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
    
    try:
        # Get published nodes from DHT storage
        all_nodes_data = await dht_publisher.kademlia_node.find_value("all_nodes")
        
        published_nodes = []
        if all_nodes_data:
            if isinstance(all_nodes_data, list):
                published_nodes.extend(all_nodes_data)
            else:
                published_nodes.append(all_nodes_data)
        
        # ALSO get contacts from routing table
        routing_contacts = dht_publisher.kademlia_node.routing_table.get_all_contacts()
        
        # Convert to unified format
        current_time = time.time()
        active_nodes = []
        seen_node_ids = set()
        
        # Process published nodes first (they have complete info)
        for node_data in published_nodes:
            if isinstance(node_data, dict):
                node_id = node_data.get('node_id')
                last_seen = node_data.get('last_seen', 0)
                
                if node_id and current_time - last_seen < 60:  # Active within last minute
                    active_nodes.append({
                        "node_id": node_id,
                        "ip": node_data.get('ip'),
                        "port": node_data.get('port'),
                        "model": node_data.get('model'),
                        "load": node_data.get('load', 0),
                        "tps": node_data.get('tps', 0),
                        "uptime": node_data.get('uptime', 0),
                        "last_seen": last_seen,
                        "source": "published"
                    })
                    seen_node_ids.add(node_id)
        
        # Add routing table contacts that aren't already included
        for contact in routing_contacts:
            if contact.node_id not in seen_node_ids and current_time - contact.last_seen < 60:
                # Try to get HTTP port and model info
                http_port = 8000  # Default assumption
                model = "unknown"
                
                # Try to detect HTTP port by testing common ports
                import requests
                for test_port in [8000, 8002, 8004]:
                    try:
                        response = requests.get(f"http://{contact.ip}:{test_port}/info", timeout=1)
                        if response.status_code == 200:
                            info = response.json()
                            http_port = test_port
                            model = info.get('model', 'unknown')
                            break
                    except:
                        continue
                
                active_nodes.append({
                    "node_id": contact.node_id,
                    "ip": contact.ip,
                    "port": http_port,
                    "model": model,
                    "load": 0.0,  # Unknown
                    "tps": 0.0,   # Unknown
                    "uptime": 0,  # Unknown
                    "last_seen": int(contact.last_seen),
                    "source": "dht_contact"
                })
                seen_node_ids.add(contact.node_id)
        
        return {
            "nodes": active_nodes,
            "total_count": len(active_nodes),
            "timestamp": current_time,
            "sources": {
                "published": len([n for n in active_nodes if n.get("source") == "published"]),
                "dht_contacts": len([n for n in active_nodes if n.get("source") == "dht_contact"])
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dht/status")
async def dht_status():
    """Get DHT network status"""
    if not dht_publisher or not dht_publisher.kademlia_node:
        raise HTTPException(status_code=503, detail="DHT not initialized")
        
    kademlia_node = dht_publisher.kademlia_node
    routing_table = kademlia_node.routing_table
    
    # Get all contacts from routing table
    all_contacts = routing_table.get_all_contacts()
    
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
