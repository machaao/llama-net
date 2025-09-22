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
    GenerationRequest, GenerationResponse,
    OpenAIModel, OpenAIModelList
)
from common.openai_models import (
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
    logger.info(f"Starting inference node with config: {config}")
    
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

app = FastAPI(title="LlamaNet Inference Node", lifespan=lifespan)

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
        return {"message": "LlamaNet Inference Node", "web_ui": "Not available", "endpoints": {
            "llamanet": ["/generate", "/status", "/info", "/health"],
            "openai": ["/v1/models", "/v1/completions", "/v1/chat/completions"]
        }}

# Original LlamaNet endpoints
@app.post("/generate", response_model=GenerationResponse)
async def generate(request: GenerationRequest):
    """Generate text from a prompt"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
        
    try:
        result = llm.generate(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            stop=request.stop,
            repeat_penalty=request.repeat_penalty
        )
        
        return GenerationResponse(
            text=result["text"],
            tokens_generated=result["tokens_generated"],
            generation_time=result["generation_time"],
            node_id=config.node_id
        )
    except Exception as e:
        logger.error(f"Generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate/stream")
async def generate_stream(request: GenerationRequest):
    """Generate text with streaming support"""
    if not llm:
        raise HTTPException(status_code=503, detail="LLM not initialized")
    
    async def stream_generator():
        try:
            async for chunk in llm.generate_stream_async(
                prompt=request.prompt,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                top_k=request.top_k,
                stop=request.stop,
                repeat_penalty=request.repeat_penalty
            ):
                # Format as Server-Sent Events
                data = {
                    "text": chunk["text"],
                    "accumulated_text": chunk["accumulated_text"],
                    "tokens_generated": chunk["tokens_generated"],
                    "generation_time": chunk["generation_time"],
                    "finished": chunk["finished"],
                    "node_id": config.node_id
                }
                yield f"data: {json.dumps(data)}\n\n"
                
                if chunk["finished"]:
                    break
        except Exception as e:
            error_data = {"error": str(e), "finished": True}
            yield f"data: {json.dumps(error_data)}\n\n"
    
    return StreamingResponse(
        stream_generator(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/plain; charset=utf-8"
        }
    )

# OpenAI-compatible endpoints
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
    
    # Convert stop to list if it's a string
    stop = None
    if request.stop:
        if isinstance(request.stop, str):
            stop = [request.stop]
        else:
            stop = request.stop
    
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
                    stop=stop,
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
            stop=stop,
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
    
    # Convert messages to a single prompt
    prompt_parts = []
    for message in request.messages:
        if message.role == "system":
            prompt_parts.append(f"System: {message.content}")
        elif message.role == "user":
            prompt_parts.append(f"User: {message.content}")
        elif message.role == "assistant":
            prompt_parts.append(f"Assistant: {message.content}")
    
    prompt = "\n".join(prompt_parts) + "\nAssistant:"
    
    # Convert stop to list if it's a string
    stop = None
    if request.stop:
        if isinstance(request.stop, str):
            stop = [request.stop]
        else:
            stop = request.stop
    
    try:
        # Handle streaming
        if request.stream:
            request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            
            async def stream_generator():
                async for chunk in llm.generate_stream_async(
                    prompt=prompt,
                    max_tokens=request.max_tokens or 100,
                    temperature=request.temperature or 0.7,
                    top_p=request.top_p or 0.9,
                    stop=stop,
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
            stop=stop,
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

# Original LlamaNet status endpoints

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
        "endpoints": {
            "llamanet": ["/generate", "/status", "/info", "/health"],
            "openai": ["/v1/models", "/v1/completions", "/v1/chat/completions"]
        }
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
        # Query DHT for all nodes
        all_nodes_data = await dht_publisher.kademlia_node.find_value("all_nodes")
        
        nodes = []
        if all_nodes_data:
            if isinstance(all_nodes_data, list):
                nodes.extend(all_nodes_data)
            else:
                nodes.append(all_nodes_data)
        
        # Filter out stale nodes and format response
        current_time = time.time()
        active_nodes = []
        
        for node_data in nodes:
            if isinstance(node_data, dict):
                last_seen = node_data.get('last_seen', 0)
                if current_time - last_seen < 60:  # Active within last minute
                    active_nodes.append({
                        "node_id": node_data.get('node_id'),
                        "ip": node_data.get('ip'),
                        "port": node_data.get('port'),
                        "model": node_data.get('model'),
                        "load": node_data.get('load', 0),
                        "tps": node_data.get('tps', 0),
                        "uptime": node_data.get('uptime', 0),
                        "last_seen": last_seen
                    })
        
        return {
            "nodes": active_nodes,
            "total_count": len(active_nodes),
            "timestamp": current_time
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
    for contact in all_contacts:
        contacts.append({
            "node_id": contact.node_id,
            "ip": contact.ip,
            "port": contact.port,
            "last_seen": contact.last_seen
        })
    
    return {
        "node_id": kademlia_node.node_id,
        "dht_port": kademlia_node.port,
        "running": kademlia_node.running,
        "contacts_count": len(all_contacts),
        "contacts": contacts,
        "storage_keys": list(kademlia_node.storage.keys()),
        "bootstrap_nodes": dht_publisher.bootstrap_nodes
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
LlamaNet Inference Node

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
  POST /v1/chat/completions     - Chat completion

LlamaNet Endpoints:
  POST /generate                - Native LlamaNet generation
  GET  /status                  - Node status and metrics
  GET  /info                    - Node information
  GET  /health                  - Health check
""")

if __name__ == "__main__":
    start_server()
