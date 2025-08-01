import os
from typing import Optional
from common.utils import load_env_var, get_logger

logger = get_logger(__name__)

class InferenceConfig:
    """Configuration for the inference node"""
    
    def __init__(self):
        # Model configuration
        self.model_path = load_env_var("MODEL_PATH", "")
        if not self.model_path:
            logger.error("MODEL_PATH environment variable is required")
            raise ValueError("MODEL_PATH environment variable is required")
            
        # Server configuration
        self.host = load_env_var("HOST", "0.0.0.0")
        self.port = int(load_env_var("PORT", 8000))
        self.node_id = load_env_var("NODE_ID", None)
        
        # Registry configuration
        self.registry_url = load_env_var("REGISTRY_URL", "http://localhost:8080")
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
            f"host={self.host}, port={self.port}, node_id={self.node_id})"
        )
