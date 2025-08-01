import os
import uuid
import argparse
import sys
import socket
from typing import Optional
from common.utils import load_env_var, get_logger

logger = get_logger(__name__)

class InferenceConfig:
    """Configuration for the inference node"""
    
    def _find_available_port(self, start_port: int = 8000) -> int:
        """Find an available TCP port starting from start_port"""
        import socket
        port = start_port
        while port < start_port + 100:  # Try up to 100 ports
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('', port))
                    return port
            except OSError:
                port += 1
        raise RuntimeError(f"No available TCP ports found starting from {start_port}")
    
    def _is_udp_port_available(self, port: int) -> bool:
        """Check if a UDP port is available"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('', port))
                return True
        except OSError:
            return False

    def _find_available_udp_port(self, start_port: int = 8001) -> int:
        """Find an available UDP port starting from start_port"""
        import socket
        port = start_port
        while port < start_port + 100:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(('', port))
                    return port
            except OSError:
                port += 1
        raise RuntimeError(f"No available UDP ports found starting from {start_port}")
    
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
            
            # Handle HTTP port
            if args.port and args.port != 8000:  # User specified a non-default port
                # Check if the specified port is available
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.bind(('', args.port))
                    self.port = args.port
                except OSError:
                    logger.warning(f"Specified port {args.port} is not available, finding alternative")
                    self.port = self._find_available_port(args.port)
                    logger.info(f"Using available port: {self.port}")
            elif load_env_var("PORT", None):
                specified_port = int(load_env_var("PORT"))
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.bind(('', specified_port))
                    self.port = specified_port
                except OSError:
                    logger.warning(f"Environment PORT {specified_port} is not available, finding alternative")
                    self.port = self._find_available_port(specified_port)
                    logger.info(f"Using available port: {self.port}")
            else:
                self.port = self._find_available_port(8000)
                logger.info(f"Using available port: {self.port}")

            # Handle DHT port  
            if args.dht_port and args.dht_port != 8001:  # User specified a non-default port
                if self._is_udp_port_available(args.dht_port):
                    self.dht_port = args.dht_port
                else:
                    logger.warning(f"Specified DHT port {args.dht_port} is not available, finding alternative")
                    self.dht_port = self._find_available_udp_port(args.dht_port)
                    logger.info(f"Using available DHT port: {self.dht_port}")
            elif load_env_var("DHT_PORT", None):
                specified_dht_port = int(load_env_var("DHT_PORT"))
                if self._is_udp_port_available(specified_dht_port):
                    self.dht_port = specified_dht_port
                else:
                    logger.warning(f"Environment DHT_PORT {specified_dht_port} is not available, finding alternative")
                    self.dht_port = self._find_available_udp_port(specified_dht_port)
                    logger.info(f"Using available DHT port: {self.dht_port}")
            else:
                self.dht_port = self._find_available_udp_port(8001)
                logger.info(f"Using available DHT port: {self.dht_port}")
                
            self.node_id = args.node_id or load_env_var("NODE_ID", uuid.uuid4().hex[:16])
            self.bootstrap_nodes = args.bootstrap_nodes or load_env_var("BOOTSTRAP_NODES", "")
        else:
            # Direct initialization (for programmatic use)
            self.model_path = model_path
            self.host = load_env_var("HOST", "0.0.0.0")
            
            # Handle HTTP port
            if load_env_var("PORT", None):
                specified_port = int(load_env_var("PORT"))
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.bind(('', specified_port))
                    self.port = specified_port
                except OSError:
                    logger.warning(f"Environment PORT {specified_port} is not available, finding alternative")
                    self.port = self._find_available_port(specified_port)
                    logger.info(f"Using available port: {self.port}")
            else:
                self.port = self._find_available_port(8000)
                logger.info(f"Using available port: {self.port}")

            # Handle DHT port
            if load_env_var("DHT_PORT", None):
                specified_dht_port = int(load_env_var("DHT_PORT"))
                if self._is_udp_port_available(specified_dht_port):
                    self.dht_port = specified_dht_port
                else:
                    logger.warning(f"Environment DHT_PORT {specified_dht_port} is not available, finding alternative")
                    self.dht_port = self._find_available_udp_port(specified_dht_port)
                    logger.info(f"Using available DHT port: {self.dht_port}")
            else:
                self.dht_port = self._find_available_udp_port(8001)
                logger.info(f"Using available DHT port: {self.dht_port}")
                
            self.node_id = load_env_var("NODE_ID", uuid.uuid4().hex[:16])
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
