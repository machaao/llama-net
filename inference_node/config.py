import os
import uuid
import argparse
import sys
from typing import Optional
from common.utils import load_env_var, get_logger

logger = get_logger(__name__)

class InferenceConfig:
    """Configuration for the inference node"""
    
    def __init__(self, model_path: str = None):
        # Parse command line arguments if model_path not provided
        if model_path is None:
            parser = argparse.ArgumentParser(description='LlamaNet Inference Node')
            parser.add_argument('--model-path', required=False, 
                              help='Path to the GGUF model file')
            parser.add_argument('--host', default='0.0.0.0',
                              help='Host to bind the inference service')
            parser.add_argument('--port', type=int, default=8000,
                              help='Port for the inference HTTP API')
            parser.add_argument('--dht-port', type=int, default=8001,
                              help='Port for Kademlia DHT protocol')
            parser.add_argument('--node-id', 
                              help='Unique identifier for this node')
            parser.add_argument('--bootstrap-nodes', default='',
                              help='Comma-separated list of bootstrap nodes (ip:port)')
            
            args = parser.parse_args()
            
            # Use command line args or fall back to environment variables
            self.model_path = args.model_path or load_env_var("MODEL_PATH", "")
            self.host = args.host or load_env_var("HOST", "0.0.0.0")
            self.port = args.port or int(load_env_var("PORT", 8000))
            self.dht_port = args.dht_port or int(load_env_var("DHT_PORT", 8001))
            self.node_id = args.node_id or load_env_var("NODE_ID", f"node-{uuid.uuid4().hex[:8]}")
            self.bootstrap_nodes = args.bootstrap_nodes or load_env_var("BOOTSTRAP_NODES", "")
        else:
            # Direct initialization (for programmatic use)
            self.model_path = model_path
            self.host = load_env_var("HOST", "0.0.0.0")
            self.port = int(load_env_var("PORT", 8000))
            self.dht_port = int(load_env_var("DHT_PORT", 8001))
            self.node_id = load_env_var("NODE_ID", f"node-{uuid.uuid4().hex[:8]}")
            self.bootstrap_nodes = load_env_var("BOOTSTRAP_NODES", "")
        
        # Validate model path
        if not self.model_path:
            logger.error("Model path is required. Use --model-path argument or MODEL_PATH environment variable")
            sys.exit(1)
        
        if not os.path.exists(self.model_path):
            logger.error(f"Model file not found: {self.model_path}")
            sys.exit(1)
            
        # DHT configuration
        self.heartbeat_interval = int(load_env_var("HEARTBEAT_INTERVAL", 10))
        
        # LLM configuration
        self.n_ctx = int(load_env_var("N_CTX", 2048))
        self.n_batch = int(load_env_var("N_BATCH", 8))
        self.n_gpu_layers = int(load_env_var("N_GPU_LAYERS", 0))
        
        # Extract model name from path
        self.model_name = os.path.basename(self.model_path).split('.')[0]
        
    def __str__(self) -> str:
        return (
            f"InferenceConfig(model_path={self.model_path}, "
            f"host={self.host}, port={self.port}, node_id={self.node_id}, "
            f"dht_port={self.dht_port})"
        )
