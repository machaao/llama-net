import uvicorn
from fastapi import FastAPI, HTTPException
from typing import Dict, List, Optional
from contextlib import asynccontextmanager

from common.models import NodeInfo
from registry.config import RegistryConfig
from registry.node_manager import NodeManager
from common.utils import get_logger

logger = get_logger(__name__)

# Global variables
config = None
node_manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global config, node_manager
    
    # Load configuration
    config = RegistryConfig()
    logger.info(f"Starting registry with config: {config}")
    
    # Initialize node manager
    node_manager = NodeManager(config)
    
    yield
    
    # Shutdown
    if node_manager:
        node_manager.stop()

app = FastAPI(title="LlamaNet Registry Service", lifespan=lifespan)

@app.post("/register")
async def register(node: NodeInfo):
    """Register or update a node"""
    if not node_manager:
        raise HTTPException(status_code=503, detail="Registry not initialized")
        
    success = node_manager.register_node(node)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to register node")
        
    return {"status": "ok", "node_id": node.node_id}

@app.get("/nodes")
async def get_nodes(model: Optional[str] = None):
    """Get all active nodes, optionally filtered by model"""
    if not node_manager:
        raise HTTPException(status_code=503, detail="Registry not initialized")
        
    nodes = node_manager.get_nodes(model)
    return {"nodes": nodes, "count": len(nodes)}

@app.get("/nodes/{node_id}")
async def get_node(node_id: str):
    """Get a specific node by ID"""
    if not node_manager:
        raise HTTPException(status_code=503, detail="Registry not initialized")
        
    node = node_manager.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
        
    return node

def start_server():
    """Start the registry server"""
    global config
    if config is None:
        config = RegistryConfig()
    
    uvicorn.run(
        "registry.server:app",
        host=config.host,
        port=config.port,
        log_level="info"
    )

if __name__ == "__main__":
    start_server()
