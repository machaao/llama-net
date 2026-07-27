import os
import uuid
import argparse
import sys
import socket
import hashlib
import time
from typing import Optional, Dict
from common.utils import load_env_var, get_logger
from common.port_utils import PortManager
from inference_node.model_manager import ModelManager
from common.context_optimizer import calculate_optimal_context

logger = get_logger(__name__)

class InferenceConfig:
    """Configuration for the inference node"""
    
    ACTIVE_MODEL_FILE = os.path.expanduser("~/.llamanet/active_model")
    POOL_HISTORY_FILE = os.path.expanduser("~/.llamanet/pool_history.json")
    
    def __init__(self, model_path: str = None):
        # Check for 'run' command in sys.argv
        if len(sys.argv) > 1 and sys.argv[1] == 'run':
            # Handle run command
            if len(sys.argv) < 3:
                logger.error("Usage: llamanet run <huggingface-url>")
                sys.exit(1)
            
            hf_url = sys.argv[2]
            model_manager = ModelManager()
            
            try:
                # Resolve model path (download if needed)
                resolved_path = model_manager.resolve_model_path(hf_url)
                
                # Update sys.argv to remove 'run' command and add --model-path
                sys.argv = [sys.argv[0]] + ['--model-path', resolved_path] + sys.argv[3:]
                
                logger.info(f"Resolved model path: {resolved_path}")
                
            except Exception as e:
                logger.error(f"Failed to resolve model: {e}")
                sys.exit(1)
        
        # Parse command line arguments if model_path not provided
        if model_path is None:
            parser = argparse.ArgumentParser(description='LlamaNet Inference Node')
            parser.add_argument('--model-path', required=False, 
                              help='Path to the GGUF model file')
            parser.add_argument('--host', default='0.0.0.0',
                              help='Host to bind the inference service')
            parser.add_argument('--port', type=int, default=8000,
                              help='Port for the inference HTTP API')
            parser.add_argument('--node-id', 
                              help='Unique identifier for this node')
            parser.add_argument('--bootstrap-peers', default='',
                              help='Comma-separated HTTP URLs of bootstrap peers '
                                   '(e.g. https://bootstrap.llamanet.app)')

            parser.add_argument('--public-ip',
                              help='Public IP address for internet-accessible nodes')

            parser.add_argument('--ctx-size', default=0, type=int, 
                                help='Context size in tokens (0 = auto-detect from model, default: 0)')

            parser.add_argument('--flash-attn', action='store_true', default=False,
                                help='Enable FlashAttention for memory-efficient inference')

            parser.add_argument('--cache-type-k', default='f16', choices=['f16', 'q8_0', 'q4_0'],
                                help='KV cache key quantization type (default: f16)')

            parser.add_argument('--cache-type-v', default='f16', choices=['f16', 'q8_0', 'q4_0'],
                                help='KV cache value quantization type (default: f16)')

            parser.add_argument('--batch-size', default=4096, type=int,
                                help='Llama Server Batch Size (in tokens)')

            parser.add_argument('--ubatch-size', default=512, type=int,
                                help='Physical µ-batch size in tokens (default: 512). '
                                     'Primary knob for prompt processing speed on single GPU.')

            parser.add_argument('--n-parallel', default=1, type=int,
                                help='Number of parallel slots (default: 1). '
                                     'Lower values improve TTFT; higher values improve throughput.')

            parser.add_argument('--threads', default=0, type=int,
                                help='CPU threads for generation (0 = auto)')

            parser.add_argument('--threads-batch', default=0, type=int,
                                help='CPU threads for batch/prefill processing (0 = auto). '
                                     'Start with physical core count.')

            parser.add_argument('--gpu-layers', default=-1, type=int,
                                help='Llama Server GPU Layers')

            parser.add_argument('--verbose', action='store_true',
                                help='Enable verbose logging for llama-cpp-python')

            parser.add_argument('--no-gpu', action='store_true',
                                help='Disable GPU acceleration (forces CPU-only mode)')

            args = parser.parse_args()
            
            # Use command line args or fall back to environment variables
            self.model_path = args.model_path or load_env_var("MODEL_PATH", "")
            self.host = args.host or load_env_var("HOST", "0.0.0.0")
            
            # KV cache and context configuration
            self.flash_attn = args.flash_attn or load_env_var("FLASH_ATTN", "false").lower() == "true"
            self.cache_type_k = load_env_var("CACHE_TYPE_K", args.cache_type_k)
            self.cache_type_v = load_env_var("CACHE_TYPE_V", args.cache_type_v)
            self.n_ctx = int(load_env_var("N_CTX", args.ctx_size))

            # Track whether context size was explicitly requested by user
            _ctx_size_from_cli = args.ctx_size > 0

            self.n_batch = int(load_env_var("N_BATCH", args.batch_size))
            self.n_ubatch = int(load_env_var("N_UBATCH", args.ubatch_size))
            self.n_parallel = int(load_env_var("N_PARALLEL", args.n_parallel))
            self.n_threads = int(load_env_var("N_THREADS", args.threads))
            self.n_threads_batch = int(load_env_var("N_THREADS_BATCH", args.threads_batch))
            # LLM configuration
            self.n_gpu_layers = int(load_env_var("N_GPU_LAYERS", args.gpu_layers))
            self.verbose = args.verbose or bool(load_env_var("VERBOSE", True))

            # Pool configuration
            self.max_models = int(load_env_var("MAX_MODELS", "0"))
            self.memory_budget_gb = float(load_env_var("MEMORY_BUDGET_GB", "0"))

            # Handle --no-gpu flag (overrides GPU layers and disables Metal)
            if args.no_gpu:
                self.n_gpu_layers = 0
                os.environ["LLAMA_NO_METAL"] = "1"
                logger.info("GPU disabled via --no-gpu flag")

            # Handle HTTP port using consolidated utilities
            preferred_http_port = args.port if args.port != 8000 else int(load_env_var("PORT", 8000))
            self.port = PortManager.get_port_with_fallback(preferred_http_port, 'tcp')

            # Generate node_id after port is determined
            self.node_id = self._load_or_generate_node_id(args.node_id, self.port)
            self.bootstrap_peers = args.bootstrap_peers or load_env_var("BOOTSTRAP_PEERS", "")

            # Public IP support for internet-accessible nodes
            self.public_ip = args.public_ip or load_env_var("PUBLIC_IP", "")
            if self.public_ip:
                logger.info(f"Configured public IP: {self.public_ip}")
        else:
            # Direct initialization (for programmatic use)
            self.model_path = model_path
            self.host = load_env_var("HOST", "0.0.0.0")
            
            # Handle HTTP port using consolidated utilities
            preferred_http_port = int(load_env_var("PORT", 8000))
            self.port = PortManager.get_port_with_fallback(preferred_http_port, 'tcp')

            # Generate node_id after port is determined
            self.node_id = self._load_or_generate_node_id(None, self.port)
            self.bootstrap_peers = load_env_var("BOOTSTRAP_PEERS", "")

            # Public IP support for internet-accessible nodes
            self.public_ip = load_env_var("PUBLIC_IP", "")
            if self.public_ip:
                logger.info(f"Configured public IP: {self.public_ip}")
            
            # KV cache and context configuration
            self.flash_attn = load_env_var("FLASH_ATTN", "false").lower() == "true"
            self.cache_type_k = load_env_var("CACHE_TYPE_K", "f16")
            self.cache_type_v = load_env_var("CACHE_TYPE_V", "f16")
            self.n_ctx = int(load_env_var("N_CTX", 0))
            
            self.n_batch = int(load_env_var("N_BATCH", 4096))
            self.n_ubatch = int(load_env_var("N_UBATCH", 512))
            self.n_parallel = int(load_env_var("N_PARALLEL", 1))
            self.n_threads = int(load_env_var("N_THREADS", 0))
            self.n_threads_batch = int(load_env_var("N_THREADS_BATCH", 0))
            self.n_gpu_layers = int(load_env_var("N_GPU_LAYERS", -1))
            self.verbose = bool(load_env_var("VERBOSE", False))

            # Pool configuration
            self.max_models = int(load_env_var("MAX_MODELS", "0"))
            self.memory_budget_gb = float(load_env_var("MEMORY_BUDGET_GB", "0"))
        
        # No-model mode support
        self.no_model_mode = False

        # Check stored active model if no model path provided
        if not self.model_path:
            stored = self._load_active_model()
            if stored and os.path.exists(stored):
                self.model_path = stored
                logger.info(f"Using stored active model: {stored}")
            else:
                self.no_model_mode = True
                self.model_path = ""
                logger.warning("No model specified - starting in no-model mode")

        # Validate model path exists (skip in no-model mode)
        if not self.no_model_mode and not os.path.exists(self.model_path):
            logger.warning(f"Model file not found: {self.model_path} - starting in no-model mode")
            self.no_model_mode = True
            self.model_path = ""

        # Auto-detect context size from model if n_ctx == 0 (or legacy default 4096)
        # Treat 4096 as "unset" ONLY when it came from env (not CLI)
        # because it was the old hardcoded default and stale N_CTX=4096
        # in shell environments would otherwise prevent auto-detection
        # of the model's native context length.
        self.n_ctx_auto_detected = False
        _should_auto_detect = self.n_ctx <= 0 or (self.n_ctx == 4096 and not _ctx_size_from_cli)
        if _should_auto_detect and self.model_path and not self.no_model_mode:
            self.n_ctx, self.n_ctx_auto_detected = calculate_optimal_context(
                self.model_path, self.cache_type_k,
                max_concurrent=max(self.max_models, 2),
            )

        if self.n_ctx <= 0:
            self.n_ctx = 4096  # Safe fallback

        # Extract model name from path
        self.model_name = os.path.basename(self.model_path) if self.model_path else "No Model Loaded"
        
        # Configure networking for better stability
        self._configure_networking()
    
    def _configure_networking(self):
        """Configure networking settings for better stability"""
        import socket

        try:
            socket.setdefaulttimeout(30)

            original_socket = socket.socket

            class _PatchedSocket(original_socket):
                """Subclass that sets SO_REUSEADDR and larger buffers on UDP sockets.

                Using a proper class (instead of a plain function) keeps
                ``isinstance(sock, socket.socket)`` valid, which is required
                by asyncio on Windows (ProactorEventLoop).
                """

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    if self.family == socket.AF_INET and self.type == socket.SOCK_DGRAM:
                        try:
                            self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            self.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                            self.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                        except OSError:
                            pass  # Some systems don't allow this

            socket.socket = _PatchedSocket

            logger.info("Network configuration applied successfully")
        except Exception as e:
            logger.warning(f"Could not configure networking: {e}")
    
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
        """Load existing node ID or generate a new one"""
        # If explicitly specified, use it
        if specified_node_id:
            logger.info(f"Using specified node ID: {specified_node_id[:16]}...")
            self._node_id_source = 'specified'
            return specified_node_id
        
        # Check environment variable
        env_node_id = load_env_var("NODE_ID", None)
        if env_node_id:
            logger.info(f"Using environment node ID: {env_node_id[:16]}...")
            self._node_id_source = 'environment'
            return env_node_id
        
        # Check stored node ID
        stored_node_id = self._get_stored_node_id()
        if stored_node_id:
            logger.info(f"Using stored node ID: {stored_node_id[:16]}...")
            self._node_id_source = 'stored'
            return stored_node_id
        
        # Generate new node ID
        node_id = self._generate_legacy_node_id(port)
        self._store_node_id(node_id)
        self._node_id_source = 'generated'
        logger.info(f"Generated new node ID: {node_id[:16]}...")
        return node_id
    
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
            "public_ip": self.public_ip or "auto-detected",
            "bootstrap_peers": self.bootstrap_peers,
            "n_ctx": self.n_ctx,
            "n_ctx_auto_detected": getattr(self, 'n_ctx_auto_detected', False),
            "n_batch": self.n_batch,
            "n_ubatch": getattr(self, 'n_ubatch', 512),
            "n_parallel": getattr(self, 'n_parallel', 1),
            "n_gpu_layers": self.n_gpu_layers,
            "flash_attn": getattr(self, 'flash_attn', False),
            "cache_type_k": getattr(self, 'cache_type_k', 'f16'),
            "cache_type_v": getattr(self, 'cache_type_v', 'f16'),
            "n_threads": getattr(self, 'n_threads', 0),
            "n_threads_batch": getattr(self, 'n_threads_batch', 0),
            "verbose": self.verbose,
        }
        
    def __str__(self) -> str:
        return (
            f"InferenceConfig(model_path={self.model_path}, "
            f"host={self.host}, port={self.port}, node_id={self.node_id}, "
            f"verbose={self.verbose}, no_model_mode={self.no_model_mode})"
        )

    def _load_active_model(self) -> Optional[str]:
        """Load stored active model path"""
        try:
            if os.path.exists(self.ACTIVE_MODEL_FILE):
                import json
                with open(self.ACTIVE_MODEL_FILE, 'r') as f:
                    data = json.load(f)
                    path = data.get("model_path", "")
                    if path and os.path.exists(path):
                        return path
        except Exception as e:
            logger.debug(f"Could not load active model: {e}")
        return None

    def save_active_model(self, model_path: str, model_name: str = ""):
        """Save the active model path for persistence across restarts"""
        try:
            import json
            data = {
                "model_path": model_path,
                "model_name": model_name or os.path.basename(model_path),
                "selected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "ui",
                "max_models": getattr(self, 'max_models', 0),
            }
            os.makedirs(os.path.dirname(self.ACTIVE_MODEL_FILE), exist_ok=True)
            with open(self.ACTIVE_MODEL_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved active model: {model_path}")
        except Exception as e:
            logger.error(f"Could not save active model: {e}")

    def load_pool_history(self) -> list:
        """Load pool history from persistent storage"""
        try:
            import json
            if os.path.exists(self.POOL_HISTORY_FILE):
                with open(self.POOL_HISTORY_FILE, 'r') as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Could not load pool history: {e}")
        return []

    def save_pool_history(self, history: list) -> None:
        """Save pool history to persistent storage"""
        try:
            import json
            os.makedirs(os.path.dirname(self.POOL_HISTORY_FILE), exist_ok=True)
            with open(self.POOL_HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=2)
            logger.debug(f"Saved pool history: {len(history)} models")
        except Exception as e:
            logger.error(f"Could not save pool history: {e}")
