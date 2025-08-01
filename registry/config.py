from common.utils import load_env_var, get_logger

logger = get_logger(__name__)

class RegistryConfig:
    """Configuration for the registry service"""
    
    def __init__(self):
        # Server configuration
        self.host = load_env_var("REGISTRY_HOST", "0.0.0.0")
        self.port = int(load_env_var("REGISTRY_PORT", 8080))
        
        # Node TTL configuration
        self.node_ttl = int(load_env_var("NODE_TTL", 30))  # seconds
        
        # Storage configuration (future: Redis, etc.)
        self.storage_type = load_env_var("STORAGE_TYPE", "memory")
        
    def __str__(self) -> str:
        return (
            f"RegistryConfig(host={self.host}, port={self.port}, "
            f"node_ttl={self.node_ttl}, storage_type={self.storage_type})"
        )
