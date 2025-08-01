import time
import threading
from typing import Dict, List, Optional
from common.models import NodeInfo
from common.utils import get_logger
from registry.config import RegistryConfig

logger = get_logger(__name__)

class NodeManager:
    """Manage inference nodes"""
    
    def __init__(self, config: RegistryConfig):
        self.config = config
        self.nodes: Dict[str, NodeInfo] = {}
        self.lock = threading.RLock()
        
        # Start cleanup thread
        self.running = True
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop)
        self.cleanup_thread.daemon = True
        self.cleanup_thread.start()
        
    def register_node(self, node: NodeInfo) -> bool:
        """Register or update a node"""
        with self.lock:
            self.nodes[node.node_id] = node
            logger.info(f"Registered node {node.node_id} ({node.model})")
            return True
            
    def get_nodes(self, model: Optional[str] = None) -> List[NodeInfo]:
        """Get all active nodes, optionally filtered by model"""
        with self.lock:
            if model:
                return [node for node in self.nodes.values() 
                        if node.model == model]
            return list(self.nodes.values())
            
    def get_node(self, node_id: str) -> Optional[NodeInfo]:
        """Get a specific node by ID"""
        with self.lock:
            return self.nodes.get(node_id)
            
    def remove_node(self, node_id: str) -> bool:
        """Remove a node"""
        with self.lock:
            if node_id in self.nodes:
                del self.nodes[node_id]
                logger.info(f"Removed node {node_id}")
                return True
            return False
            
    def _cleanup_loop(self):
        """Periodically clean up stale nodes"""
        while self.running:
            self._cleanup_stale_nodes()
            time.sleep(5)  # Check every 5 seconds
            
    def _cleanup_stale_nodes(self):
        """Remove nodes that haven't sent a heartbeat recently"""
        current_time = int(time.time())
        stale_nodes = []
        
        with self.lock:
            for node_id, node in self.nodes.items():
                if current_time - node.last_seen > self.config.node_ttl:
                    stale_nodes.append(node_id)
                    
            for node_id in stale_nodes:
                del self.nodes[node_id]
                logger.info(f"Removed stale node {node_id}")
                
    def stop(self):
        """Stop the node manager"""
        self.running = False
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=1)
