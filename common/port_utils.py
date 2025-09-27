import socket
from typing import List, Tuple, Optional
from common.utils import get_logger

logger = get_logger(__name__)

class PortManager:
    """Centralized port management utilities"""
    
    @staticmethod
    def is_tcp_port_available(port: int, host: str = '') -> bool:
        """Check if a TCP port is available"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return True
        except OSError:
            return False
    
    @staticmethod
    def is_udp_port_available(port: int, host: str = '') -> bool:
        """Check if a UDP port is available"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return True
        except OSError:
            return False
    
    @staticmethod
    def find_available_tcp_port(start_port: int = 8000, max_attempts: int = 100) -> int:
        """Find an available TCP port"""
        for port in range(start_port, start_port + max_attempts):
            if PortManager.is_tcp_port_available(port):
                return port
        raise RuntimeError(f"No available TCP ports found starting from {start_port}")
    
    @staticmethod
    def find_available_udp_port(start_port: int = 8001, max_attempts: int = 100) -> int:
        """Find an available UDP port"""
        for port in range(start_port, start_port + max_attempts):
            if PortManager.is_udp_port_available(port):
                return port
        raise RuntimeError(f"No available UDP ports found starting from {start_port}")
    
    @staticmethod
    def get_port_with_fallback(preferred_port: int, port_type: str = 'tcp') -> int:
        """Get preferred port or find alternative"""
        check_func = PortManager.is_tcp_port_available if port_type == 'tcp' else PortManager.is_udp_port_available
        find_func = PortManager.find_available_tcp_port if port_type == 'tcp' else PortManager.find_available_udp_port
        
        if check_func(preferred_port):
            return preferred_port
        else:
            logger.warning(f"Port {preferred_port} not available, finding alternative")
            alternative = find_func(preferred_port)
            logger.info(f"Using alternative {port_type.upper()} port: {alternative}")
            return alternative
