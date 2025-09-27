import os
import uuid
import argparse
import sys
import socket
import hashlib
from typing import Optional, Dict
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
            
            # Handle HTTP port using consolidated utilities
            preferred_http_port = args.port if args.port != 8000 else int(load_env_var("PORT", 8000))
            self.port = PortManager.get_port_with_fallback(preferred_http_port, 'tcp')

            # Handle DHT port using consolidated utilities  
            preferred_dht_port = args.dht_port if args.dht_port != 8001 else int(load_env_var("DHT_PORT", 8001))
            self.dht_port = PortManager.get_port_with_fallback(preferred_dht_port, 'udp')
                
            # Generate hardware-based node_id after port is determined
            self.node_id = self._load_or_generate_node_id(args.node_id, self.port)
            self.bootstrap_nodes = args.bootstrap_nodes or load_env_var("BOOTSTRAP_NODES", "")
        else:
            # Direct initialization (for programmatic use)
            self.model_path = model_path
            self.host = load_env_var("HOST", "0.0.0.0")
            
            # Handle HTTP port using consolidated utilities
            preferred_http_port = int(load_env_var("PORT", 8000))
            self.port = PortManager.get_port_with_fallback(preferred_http_port, 'tcp')

            # Handle DHT port using consolidated utilities
            preferred_dht_port = int(load_env_var("DHT_PORT", 8001))
            self.dht_port = PortManager.get_port_with_fallback(preferred_dht_port, 'udp')
                
            # Generate hardware-based node_id after port is determined
            self.node_id = self._load_or_generate_node_id(None, self.port)
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
        self.n_gpu_layers = int(load_env_var("N_GPU_LAYERS", -1))
        
        # Extract model name from path
        self.model_name = os.path.basename(self.model_path).split('.')[0]
        
        # Configure networking for better stability
        self._configure_networking()
    
    def _configure_networking(self):
        """Configure networking settings for better stability"""
        import socket
        
        try:
            # Set socket options for better UDP handling
            socket.setdefaulttimeout(30)
            
            # Configure socket reuse
            original_socket = socket.socket
            def patched_socket(*args, **kwargs):
                sock = original_socket(*args, **kwargs)
                if sock.family == socket.AF_INET and sock.type == socket.SOCK_DGRAM:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    # Set buffer sizes for UDP
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                    except OSError:
                        pass  # Some systems don't allow this
                return sock
            socket.socket = patched_socket
            
            logger.info("Network configuration applied successfully")
        except Exception as e:
            logger.warning(f"Could not configure networking: {e}")
    
    def _generate_hardware_based_node_id(self, port: int) -> str:
        """Generate a hardware-based node ID"""
        try:
            from common.hardware_fingerprint import HardwareFingerprint
            fingerprint = HardwareFingerprint()
            node_id = fingerprint.generate_node_id(port)
            
            # Log hardware fingerprint summary for debugging
            summary = fingerprint.get_fingerprint_summary()
            logger.info(f"Hardware fingerprint summary: {summary}")
            
            return node_id
        except Exception as e:
            logger.warning(f"Failed to generate hardware-based node ID: {e}")
            # Fallback to legacy method
            return self._generate_legacy_node_id(port)
    
    def _generate_legacy_node_id(self, port: int) -> str:
        """Generate a legacy node ID as fallback"""
        from common.utils import get_host_ip
        
        # Get host information for uniqueness
        host_ip = get_host_ip()
        
        # Create a unique string combining host info, port, and random component
        unique_string = f"{host_ip}:{port}:{uuid.uuid4().hex[:8]}"
        
        # Generate SHA-1 hash (160-bit) for Kademlia compatibility
        node_hash = hashlib.sha1(unique_string.encode()).hexdigest()
        
        logger.info(f"Generated legacy node_id: {node_hash[:16]}... for {host_ip}:{port}")
        return node_hash
    
    def _load_or_generate_node_id(self, specified_node_id: Optional[str], port: int) -> str:
        """Load existing node ID or generate a new hardware-based one"""
        # If explicitly specified, use it
        if specified_node_id:
            logger.info(f"Using specified node ID: {specified_node_id[:16]}...")
            return specified_node_id
        
        # Check environment variable
        env_node_id = load_env_var("NODE_ID", None)
        if env_node_id:
            logger.info(f"Using environment node ID: {env_node_id[:16]}...")
            return env_node_id
        
        # Generate hardware-based node ID
        try:
            from common.hardware_fingerprint import HardwareFingerprint
            fingerprint = HardwareFingerprint()
            node_id = fingerprint.generate_node_id(port)
            
            # Set source for debugging
            self._node_id_source = 'hardware_based'
            
            # Validate consistency if we have a stored ID
            stored_node_id = self._get_stored_node_id()
            if stored_node_id:
                if fingerprint.validate_consistency(stored_node_id, port):
                    logger.info(f"Using consistent stored node ID: {stored_node_id[:16]}...")
                    self._node_id_source = 'stored_hardware_based'
                    return stored_node_id
                else:
                    logger.warning("Hardware fingerprint changed, generating new node ID")
                    # Store the new node ID
                    self._store_node_id(node_id)
                    self._node_id_source = 'hardware_based_updated'
            else:
                # First time, store the generated ID
                self._store_node_id(node_id)
                self._node_id_source = 'hardware_based_new'
            
            return node_id
            
        except Exception as e:
            logger.error(f"Failed to generate hardware-based node ID: {e}")
            # Fallback to legacy method
            self._node_id_source = 'legacy_fallback'
            return self._generate_legacy_node_id(port)
    
    def _get_stored_node_id(self) -> Optional[str]:
        """Get stored node ID from persistent storage"""
        try:
            node_id_file = os.path.expanduser("~/.llamanet_node_id")
            if os.path.exists(node_id_file):
                with open(node_id_file, 'r') as f:
                    stored_id = f.read().strip()
                    if stored_id and len(stored_id) == 40:  # SHA-1 hex length
                        return stored_id
        except Exception as e:
            logger.debug(f"Could not read stored node ID: {e}")
        return None
    
    def _store_node_id(self, node_id: str) -> None:
        """Store node ID to persistent storage"""
        try:
            node_id_file = os.path.expanduser("~/.llamanet_node_id")
            with open(node_id_file, 'w') as f:
                f.write(node_id)
            logger.debug(f"Stored node ID to {node_id_file}")
        except Exception as e:
            logger.warning(f"Could not store node ID: {e}")
    
    def get_hardware_info(self) -> Dict:
        """Get hardware fingerprint information for debugging"""
        try:
            from common.hardware_fingerprint import HardwareFingerprint
            fingerprint = HardwareFingerprint()
            return fingerprint.get_fingerprint_summary()
        except Exception as e:
            logger.warning(f"Could not get hardware info: {e}")
            return {"error": str(e)}
    
    def _validate_node_id_format(self, node_id: str) -> bool:
        """Validate that node_id is a valid hex string of correct length"""
        try:
            if not node_id or not isinstance(node_id, str):
                return False
            
            # Should be a valid hex string (SHA-1 = 40 characters)
            if len(node_id) != 40:
                logger.warning(f"Node ID length is {len(node_id)}, expected 40 characters")
                return False
                
            int(node_id, 16)  # Test if it's valid hex
            return True
        except (ValueError, TypeError) as e:
            logger.warning(f"Node ID validation failed: {e}")
            return False

    def _get_node_id_source(self) -> str:
        """Get the source of the current node ID for debugging"""
        return getattr(self, '_node_id_source', 'hardware_based')

    def get_configuration_summary(self) -> Dict:
        """Get a summary of the current configuration for debugging"""
        return {
            "node_id": self.node_id[:16] + "...",
            "node_id_source": self._get_node_id_source(),
            "model_name": self.model_name,
            "model_path": self.model_path,
            "host": self.host,
            "port": self.port,
            "dht_port": self.dht_port,
            "bootstrap_nodes": self.bootstrap_nodes,
            "heartbeat_interval": self.heartbeat_interval,
            "hardware_based": True
        }
        
    def __str__(self) -> str:
        return (
            f"InferenceConfig(model_path={self.model_path}, "
            f"host={self.host}, port={self.port}, node_id={self.node_id}, "
            f"dht_port={self.dht_port})"
        )
