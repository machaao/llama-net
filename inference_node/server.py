import asyncio
import uvicorn
from fastapi import FastAPI, HTTPException
from typing import Dict, Any
from contextlib import asynccontextmanager

from common.models import GenerationRequest, GenerationResponse
from inference_node.config import InferenceConfig
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.metrics import SystemInfo
from inference_node.dht_publisher import DHTPublisher
from common.utils import get_logger

logger = get_logger(__name__)

# Global variables
config = None
llm = None
dht_publisher = None
system_info = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global config, llm, dht_publisher, system_info
    
    # Load configuration
    config = InferenceConfig()
    logger.info(f"Starting inference node with config: {config}")
    
    # Initialize LLM
    llm = LlamaWrapper(config)
    
    # Get system info
    system_info = SystemInfo.get_all_info()
    
    # Start DHT publisher
    dht_publisher = DHTPublisher(config, llm.get_metrics)
    await dht_publisher.start()
    
    yield
    
    # Shutdown
    if dht_publisher:
        await dht_publisher.stop()

app = FastAPI(title="LlamaNet Inference Node", lifespan=lifespan)

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
        "dht_port": config.dht_port
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
""")

if __name__ == "__main__":
    start_server()
