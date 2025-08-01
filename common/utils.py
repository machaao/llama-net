import os
import json
import logging
from typing import Any, Dict, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name"""
    return logging.getLogger(name)

def load_env_var(key: str, default: Any = None) -> Any:
    """Load an environment variable with a default value"""
    return os.environ.get(key, default)

def get_host_ip() -> str:
    """Get the host IP address"""
    # This is a simple implementation - in production you might want something more robust
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip
