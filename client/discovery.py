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
