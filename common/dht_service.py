import asyncio
from typing import Optional, List, Tuple
from dht.kademlia_node import KademliaNode
from common.utils import get_logger

logger = get_logger(__name__)

class SharedDHTService:
    """Thread-safe singleton DHT service shared across server and client components"""
    
    _instance: Optional['SharedDHTService'] = None
    _kademlia_node: Optional[KademliaNode] = None
    _initialized = False
    _initializing = False
    _lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @property
    def kademlia_node(self) -> Optional[KademliaNode]:
        return self._kademlia_node
    
    async def initialize(self, node_id: str, port: int, bootstrap_nodes: List[Tuple[str, int]] = None):
        """Thread-safe initialization of the shared DHT node"""
        async with self._lock:
            if self._initialized:
                logger.debug("DHT service already initialized")
                return self._kademlia_node
            
            if self._initializing:
                # Wait for ongoing initialization
                while self._initializing and not self._initialized:
                    await asyncio.sleep(0.1)
                return self._kademlia_node
            
            self._initializing = True
            
            try:
                logger.info(f"Initializing shared DHT service on port {port}")
                self._kademlia_node = KademliaNode(node_id=node_id, port=port)
                await self._kademlia_node.start(bootstrap_nodes or [])
                self._initialized = True
                
                logger.info(f"Shared DHT service initialized on port {self._kademlia_node.port}")
                return self._kademlia_node
                
            except Exception as e:
                logger.error(f"Failed to initialize DHT service: {e}")
                self._kademlia_node = None
                raise
            finally:
                self._initializing = False
    
    async def stop(self):
        """Thread-safe cleanup of the shared DHT service"""
        async with self._lock:
            if self._kademlia_node and self._initialized:
                try:
                    await self._kademlia_node.stop()
                    logger.info("Shared DHT service stopped")
                except Exception as e:
                    logger.error(f"Error stopping DHT service: {e}")
                finally:
                    self._kademlia_node = None
                    self._initialized = False
                    self._initializing = False
    
    def is_initialized(self) -> bool:
        return self._initialized and self._kademlia_node is not None
