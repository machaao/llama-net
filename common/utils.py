import os
import json
import logging
import socket
import ipaddress
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

def is_private_ip(ip: str) -> bool:
    """Check if an IP address is a private/reserved address (RFC 1918, loopback, link-local)"""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return True


def detect_public_ip() -> Optional[str]:
    """Detect public IP by scanning local network interfaces.
    
    Returns the first public (non-private) IP found, or None if behind NAT.
    """
    logger = get_logger(__name__)
    
    try:
        # Try psutil interface scanning first (most reliable)
        try:
            import psutil
            for name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        if not is_private_ip(ip):
                            logger.info(f"Detected public IP: {ip} (from interface {name})")
                            return ip
        except (ImportError, Exception) as e:
            logger.debug(f"psutil scan skipped: {e}")
        
        # Fallback: bind to outbound socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            if not is_private_ip(ip):
                logger.info(f"Detected public IP: {ip} (from outbound socket)")
                return ip
        except Exception:
            pass
        finally:
            s.close()
        
        return None
        
    except Exception as e:
        logger.debug(f"Public IP detection failed: {e}")
        return None


def get_host_ip(public_ip: Optional[str] = None) -> str:
    """Get the host IP address.
    
    Priority:
      1. Explicit public_ip parameter (from CLI/env)
      2. Auto-detected public IP from network interfaces
      3. Private LAN IP from outbound socket
      4. 127.0.0.1 as last resort
    """
    logger = get_logger(__name__)
    
    # Priority 1: Explicit public IP from user
    if public_ip:
        logger.info(f"Using configured public IP: {public_ip}")
        return public_ip
    
    # Priority 2: Auto-detect public IP from interfaces
    detected_public = detect_public_ip()
    if detected_public:
        return detected_public
    
    # Priority 3: Private LAN IP (standard method)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    
    if is_private_ip(ip):
        logger.warning(f"All network interfaces have private IPs (behind NAT?)")
        logger.warning(f"Detected: {ip} (private)")
        logger.warning(f"For internet-accessible nodes, set PUBLIC_IP env var:")
        logger.warning(f"  export PUBLIC_IP=<your-public-ip>")
        logger.warning(f"  or use: --public-ip <your-public-ip>")
    
    return ip

def normalize_stop_tokens(stop):
    """Normalize stop tokens to the format expected by llama-cpp-python"""
    if stop is None:
        return None
    elif isinstance(stop, str):
        return [stop] if stop.strip() else None
    elif isinstance(stop, list):
        # Filter out empty strings and ensure all items are strings
        normalized = [str(token).strip() for token in stop if str(token).strip()]
        return normalized if normalized else None
    else:
        return None
