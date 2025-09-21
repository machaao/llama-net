import requests
from typing import List, Optional, Dict, Any
import time
import random
from common.models import NodeInfo
from common.utils import get_logger

logger = get_logger(__name__)

class RegistryClient:
    """Client for the registry service"""
    
    def __init__(self, registry_url: str = "http://localhost:8080"):
        self.registry_url = registry_url
        self.cache_time = 0
        self.cache_ttl = 5  # Cache results for 5 seconds
        self.nodes_cache: List[NodeInfo] = []
        
    def get_nodes(self, model: Optional[str] = None, force_refresh: bool = False) -> List[NodeInfo]:
        """Get all active nodes, optionally filtered by model"""
        current_time = time.time()
        
        # Check if we need to refresh the cache
        if force_refresh or current_time - self.cache_time > self.cache_ttl:
            self._refresh_nodes()
            
        # Filter by model if needed
        if model:
            return [node for node in self.nodes_cache if node.model == model]
        return self.nodes_cache
        
    def _refresh_nodes(self):
        """Refresh the nodes cache"""
        try:
            response = requests.get(f"{self.registry_url}/nodes")
            if response.status_code == 200:
                data = response.json()
                # Convert dict to NodeInfo objects
                self.nodes_cache = [NodeInfo(**node) for node in data["nodes"]]
                self.cache_time = time.time()
                logger.debug(f"Refreshed nodes cache, found {len(self.nodes_cache)} nodes")
            else:
                logger.warning(f"Failed to get nodes: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Error refreshing nodes: {e}")
from abc import ABC, abstractmethod
from typing import List, Optional
from common.models import NodeInfo

class DiscoveryInterface(ABC):
    """Abstract base class for node discovery mechanisms"""
    
    @abstractmethod
    async def start(self):
        """Start the discovery service"""
        pass
    
    @abstractmethod
    async def stop(self):
        """Stop the discovery service"""
        pass
    
    @abstractmethod
    async def get_nodes(self, model: Optional[str] = None, force_refresh: bool = False) -> List[NodeInfo]:
        """Get available nodes, optionally filtered by model"""
        pass
    
    @abstractmethod
    async def find_specific_node(self, node_id: str) -> Optional[NodeInfo]:
        """Find a specific node by ID"""
        pass
