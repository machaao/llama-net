import time
import ipaddress
from typing import Dict, Any, Optional, List
from common.utils import get_logger

logger = get_logger(__name__)

class NodeValidator:
    """Centralized node validation utilities"""
    
    @staticmethod
    def validate_node_id(node_id: str) -> bool:
        """Validate node ID format"""
        try:
            if not node_id or not isinstance(node_id, str):
                return False
            if len(node_id) != 40:  # SHA-1 hex length
                return False
            int(node_id, 16)  # Test if valid hex
            return True
        except (ValueError, TypeError):
            return False
    
    @staticmethod
    def validate_contact(contact) -> bool:
        """Validate DHT contact"""
        try:
            if not contact.node_id or not contact.ip:
                return False
            if not NodeValidator.validate_node_id(contact.node_id):
                return False
            ipaddress.IPv4Address(contact.ip)  # Validate IP
            if hasattr(contact, 'last_seen'):
                if time.time() - contact.last_seen > 300:  # 5 minutes
                    return False
            return True
        except Exception:
            return False
    
    @staticmethod
    def validate_node_info(node_info: Dict[str, Any]) -> bool:
        """Validate node info structure"""
        required_fields = ['node_id', 'ip', 'port', 'model']
        
        for field in required_fields:
            if field not in node_info:
                return False
        
        if not NodeValidator.validate_node_id(node_info['node_id']):
            return False
        
        try:
            ipaddress.IPv4Address(node_info['ip'])
            port = int(node_info['port'])
            if not (1024 <= port <= 65535):
                return False
        except (ValueError, ipaddress.AddressValueError):
            return False
        
        return True

class NetworkValidator:
    """Network-level validation utilities"""
    
    @staticmethod
    def check_duplicate_endpoints(nodes: List[Dict[str, Any]]) -> List[str]:
        """Check for duplicate IP:port combinations"""
        seen_endpoints = {}
        duplicates = []
        
        for node in nodes:
            endpoint = f"{node.get('ip')}:{node.get('port')}"
            node_id = node.get('node_id')
            
            if endpoint in seen_endpoints:
                existing_node = seen_endpoints[endpoint]
                if existing_node != node_id:
                    duplicates.append(f"Duplicate endpoint {endpoint}: {existing_node[:8]}... vs {node_id[:8]}...")
            else:
                seen_endpoints[endpoint] = node_id
        
        return duplicates
