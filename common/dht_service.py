import asyncio
from typing import Optional, List, Tuple
from dht.kademlia_node import KademliaNode
from common.utils import get_logger

logger = get_logger(__name__)

class SharedDHTService:
    """Singleton DHT service shared across server and client components"""
    
    _instance: Optional['SharedDHTService'] = None
    _kademlia_node: Optional[KademliaNode] = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @property
    def kademlia_node(self) -> Optional[KademliaNode]:
        return self._kademlia_node
    
    async def initialize(self, node_id: str, port: int, bootstrap_nodes: List[Tuple[str, int]] = None):
        """Initialize the shared DHT node"""
        if self._initialized:
            logger.debug("DHT service already initialized")
            return self._kademlia_node
        
        self._kademlia_node = KademliaNode(node_id=node_id, port=port)
        await self._kademlia_node.start(bootstrap_nodes or [])
        self._initialized = True
        
        logger.info(f"Shared DHT service initialized on port {self._kademlia_node.port}")
        return self._kademlia_node
    
    async def stop(self):
        """Stop the shared DHT service"""
        if self._kademlia_node:
            await self._kademlia_node.stop()
            self._kademlia_node = None
            self._initialized = False
            logger.info("Shared DHT service stopped")
    
    def is_initialized(self) -> bool:
        return self._initialized and self._kademlia_node is not None
