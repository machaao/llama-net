import asyncio
import uvicorn
from fastapi import FastAPI, HTTPException
from typing import Dict, Any

from common.models import GenerationRequest, GenerationResponse
from inference_node.config import InferenceConfig
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.metrics import SystemInfo
from inference_node.dht_publisher import DHTPublisher
from common.utils import get_logger

logger = get_logger(__name__)

app = FastAPI(title="LlamaNet Inference Node")

# Global variables
config = None
llm = None
dht_publisher = None
system_info = None

@app.on_event("startup")
async def startup_event():
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

@app.on_event("shutdown")
async def shutdown_event():
    if dht_publisher:
        await dht_publisher.stop()

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
    uvicorn.run(
        "inference_node.server:app",
        host=config.host,
        port=config.port,
        log_level="info"
    )

if __name__ == "__main__":
    start_server()
