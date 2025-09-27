import asyncio
from typing import Optional, List, Tuple, Dict, Any
from dht.kademlia_node import KademliaNode
from common.utils import get_logger

logger = get_logger(__name__)

class SharedDHTService:
    """Thread-safe singleton DHT service shared across server and client components with hardware-based node ID support"""
    
    _instance: Optional['SharedDHTService'] = None
    _kademlia_node: Optional[KademliaNode] = None
    _initialized = False
    _initializing = False
    _lock = asyncio.Lock()
    _event_publisher = None  # Reference to event publisher
    
    # Store initialization parameters for validation
    _node_id: Optional[str] = None
    _port: Optional[int] = None
    _bootstrap_nodes: List[Tuple[str, int]] = []
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @property
    def kademlia_node(self) -> Optional[KademliaNode]:
        return self._kademlia_node
    
    def set_event_publisher(self, event_publisher):
        """Set the event publisher for broadcasting DHT events"""
        self._event_publisher = event_publisher
        logger.debug("Event publisher registered with DHT service")
    
    async def initialize(self, node_id: str, port: int, bootstrap_nodes: List[Tuple[str, int]] = None):
        """Thread-safe initialization of the shared DHT node with hardware-based node ID validation"""
        async with self._lock:
            if self._initialized:
                # Validate that we're using the same hardware-based node ID
                if self._kademlia_node and self._kademlia_node.node_id != node_id:
                    logger.warning(f"DHT service node ID mismatch detected!")
                    logger.warning(f"  Existing DHT node ID: {self._kademlia_node.node_id[:16]}...")
                    logger.warning(f"  Requested node ID: {node_id[:16]}...")
                    logger.warning("This indicates hardware changes or configuration inconsistency")
                    
                    # Stop existing node and reinitialize with correct hardware-based ID
                    logger.info("Reinitializing DHT service with updated hardware-based node ID...")
                    await self._stop_internal()
                    self._initialized = False
                else:
                    logger.debug("DHT service already initialized with correct node ID")
                    return self._kademlia_node
            
            if self._initializing:
                # Wait for ongoing initialization
                while self._initializing and not self._initialized:
                    await asyncio.sleep(0.1)
                return self._kademlia_node
            
            self._initializing = True
            
            try:
                logger.info(f"Initializing shared DHT service with hardware-based node ID: {node_id[:16]}...")
                
                # Store initialization parameters
                self._node_id = node_id
                self._port = port
                self._bootstrap_nodes = bootstrap_nodes or []
                
                # Validate node ID format before creating Kademlia node
                if not self._validate_node_id_format(node_id):
                    raise ValueError(f"Invalid hardware-based node ID format: {node_id}")
                
                # Create Kademlia node with explicit hardware-based node ID
                self._kademlia_node = KademliaNode(node_id=node_id, port=port)
                
                # CRITICAL: Validate that the Kademlia node accepted the hardware-based node ID
                if self._kademlia_node.node_id != node_id:
                    logger.error(f"CRITICAL: KademliaNode failed to use provided hardware-based node ID!")
                    logger.error(f"  Provided: {node_id[:16]}...")
                    logger.error(f"  KademliaNode actual: {self._kademlia_node.node_id[:16]}...")
                    
                    # Force the correct node ID
                    logger.warning("Forcing KademliaNode to use hardware-based node ID...")
                    self._kademlia_node.node_id = node_id
                    
                    # Update routing table to use correct node ID
                    if self._kademlia_node.routing_table:
                        self._kademlia_node.routing_table.node_id = node_id
                    
                    logger.info("KademliaNode corrected to use hardware-based node ID")
                
                # Start with enhanced bootstrap event handling
                await self._start_with_bootstrap_events()
                
                # Final validation after startup
                if self._kademlia_node.node_id != node_id:
                    logger.error(f"CRITICAL: Node ID changed during startup!")
                    logger.error(f"  Expected: {node_id[:16]}...")
                    logger.error(f"  After startup: {self._kademlia_node.node_id[:16]}...")
                    
                    # Force correct node ID again
                    self._kademlia_node.node_id = node_id
                    if self._kademlia_node.routing_table:
                        self._kademlia_node.routing_table.node_id = node_id
                    
                    logger.warning("Node ID corrected after startup")
                
                self._initialized = True
                
                logger.info(f"✅ Shared DHT service initialized successfully with hardware-based node ID: {node_id[:16]}...")
                logger.info(f"DHT running on port: {self._kademlia_node.port}")
                
                # Log bootstrap information
                if self._bootstrap_nodes:
                    logger.info(f"🌐 Connected to bootstrap nodes: {self._bootstrap_nodes}")
                else:
                    logger.info("🌟 Running as bootstrap node")
                
                return self._kademlia_node
                
            except Exception as e:
                logger.error(f"Failed to initialize shared DHT service: {e}")
                self._kademlia_node = None
                raise
            finally:
                self._initializing = False
    
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
    
    async def _start_with_bootstrap_events(self):
        """Start Kademlia node with enhanced bootstrap event handling (NO JOIN EVENTS)"""
        if not self._bootstrap_nodes:
            # Starting as bootstrap node
            await self._kademlia_node.start([])
            
            if self._event_publisher:
                await self._event_publisher._broadcast_node_event("bootstrap_node_started", {
                    'node_id': self._node_id,
                    'port': self._port,
                    'dht_port': self._kademlia_node.port,
                    'role': 'bootstrap_node',
                    'event_type': 'bootstrap_only'  # Clearly mark as bootstrap event
                })
            
            logger.info("🌟 Started as bootstrap node")
        else:
            # Joining existing network
            logger.info(f"🔗 Joining network via {len(self._bootstrap_nodes)} bootstrap nodes")
            
            if self._event_publisher:
                await self._event_publisher._broadcast_node_event("bootstrap_join_initiated", {
                    'node_id': self._node_id,
                    'bootstrap_nodes': self._bootstrap_nodes,
                    'join_method': 'enhanced_bootstrap',
                    'event_type': 'bootstrap_only'  # Clearly mark as bootstrap event
                })
            
            await self._kademlia_node.start(self._bootstrap_nodes)
            
            # Verify bootstrap success
            contacts = self._kademlia_node.routing_table.get_all_contacts()
            
            if self._event_publisher:
                await self._event_publisher._broadcast_node_event("bootstrap_join_completed", {
                    'node_id': self._node_id,
                    'successful_connections': len(contacts),
                    'bootstrap_nodes': self._bootstrap_nodes,
                    'join_success': len(contacts) > 0,
                    'event_type': 'bootstrap_only'  # Clearly mark as bootstrap event
                })
            
            if len(contacts) > 0:
                logger.info(f"✅ Successfully joined network with {len(contacts)} initial contacts")
            else:
                logger.warning("⚠️ Joined network but no contacts established")
    
    async def _stop_internal(self):
        """Internal stop method without lock (called from within locked context)"""
        if self._kademlia_node:
            try:
                await self._kademlia_node.stop()
                logger.debug("Kademlia node stopped")
            except Exception as e:
                logger.error(f"Error stopping Kademlia node: {e}")
            finally:
                self._kademlia_node = None
        
        self._initialized = False
        self._initializing = False
    
    async def stop(self):
        """Thread-safe cleanup of the shared DHT service"""
        async with self._lock:
            if self._initialized or self._kademlia_node:
                logger.info("Stopping shared DHT service...")
                await self._stop_internal()
                logger.info("Shared DHT service stopped")
    
    async def fast_stop(self):
        """Fast stop for uvicorn compatibility - don't wait for cleanup"""
        async with self._lock:
            if self._kademlia_node:
                try:
                    # Set running to False immediately
                    self._kademlia_node.running = False
                    
                    # Cancel any running tasks without waiting
                    if hasattr(self._kademlia_node, 'server_task'):
                        if self._kademlia_node.server_task and not self._kademlia_node.server_task.done():
                            self._kademlia_node.server_task.cancel()
                    
                    # Don't wait for socket cleanup - let OS handle it
                    logger.debug("DHT fast stop completed")
                    
                except Exception as e:
                    logger.debug(f"Error in DHT fast stop: {e}")
                finally:
                    self._kademlia_node = None
            
            self._initialized = False
            self._initializing = False
    
    def is_initialized(self) -> bool:
        """Check if the DHT service is properly initialized"""
        return self._initialized and self._kademlia_node is not None
    
    def get_node_id(self) -> Optional[str]:
        """Get the current hardware-based node ID"""
        return self._node_id
    
    def validate_node_id(self, expected_node_id: str) -> bool:
        """Validate that the DHT service is using the expected hardware-based node ID"""
        if not self.is_initialized():
            logger.debug("Cannot validate node ID: DHT service not initialized")
            return False
        
        actual_node_id = self._kademlia_node.node_id
        is_valid = actual_node_id == expected_node_id
        
        if not is_valid:
            logger.warning(f"Hardware-based node ID validation failed:")
            logger.warning(f"  Expected: {expected_node_id[:16]}...")
            logger.warning(f"  Actual: {actual_node_id[:16]}...")
        else:
            logger.debug("✅ Hardware-based node ID validation passed")
        
        return is_valid
    
    async def update_node_id(self, new_node_id: str):
        """Update the node ID due to hardware changes (requires restart of DHT service)"""
        async with self._lock:
            if self.is_initialized():
                logger.info(f"Updating DHT service node ID due to hardware changes:")
                logger.info(f"  From: {self._node_id[:16]}...")
                logger.info(f"  To: {new_node_id[:16]}...")
                
                # Store old bootstrap nodes
                old_bootstrap_nodes = self._bootstrap_nodes
                old_port = self._port
                
                # Stop current service
                await self._stop_internal()
                
                # Reinitialize with new hardware-based node ID
                await self.initialize(new_node_id, old_port, old_bootstrap_nodes)
                
                logger.info("✅ DHT service node ID updated successfully")
            else:
                logger.warning("Cannot update node ID: DHT service not initialized")
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive status of the shared DHT service"""
        status = {
            "initialized": self._initialized,
            "initializing": self._initializing,
            "node_id": self._node_id[:16] + "..." if self._node_id else None,
            "port": self._port,
            "bootstrap_nodes": self._bootstrap_nodes,
            "kademlia_running": False,
            "actual_node_id": None,
            "node_id_consistent": False,
            "hardware_based": True
        }
        
        if self._kademlia_node:
            status.update({
                "kademlia_running": self._kademlia_node.running,
                "actual_node_id": self._kademlia_node.node_id[:16] + "..." if self._kademlia_node.node_id else None,
                "actual_port": self._kademlia_node.port,
                "node_id_consistent": self.validate_node_id(self._node_id) if self._node_id else False
            })
        
        return status
    
    async def coordinate_routing_table_updates(self):
        """Set up coordination between DHT events and routing table updates"""
        if not self.is_initialized():
            logger.warning("Cannot coordinate routing updates: DHT service not initialized")
            return
        
        try:
            # Set up periodic routing table refresh
            async def periodic_routing_refresh():
                while self._kademlia_node and self._kademlia_node.running:
                    try:
                        await self._kademlia_node.refresh_routing_table_from_network()
                        await asyncio.sleep(120)  # Refresh every 2 minutes
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.error(f"Error in periodic routing refresh: {e}")
                        await asyncio.sleep(30)  # Wait before retry
            
            # Start the refresh task
            asyncio.create_task(periodic_routing_refresh())
            
            logger.info("✅ DHT routing table coordination established")
            
        except Exception as e:
            logger.error(f"Failed to coordinate routing table updates: {e}")

    async def force_node_id_correction(self, correct_node_id: str):
        """Force correction of node ID if there's a mismatch (emergency fix)"""
        async with self._lock:
            if not self.is_initialized():
                logger.warning("Cannot force correction: DHT service not initialized")
                return False
            
            if self._kademlia_node.node_id != correct_node_id:
                logger.warning(f"Forcing node ID correction:")
                logger.warning(f"  Current: {self._kademlia_node.node_id[:16]}...")
                logger.warning(f"  Correct: {correct_node_id[:16]}...")
                
                # Update the node ID directly
                self._kademlia_node.node_id = correct_node_id
                self._node_id = correct_node_id
                
                # Update routing table
                if self._kademlia_node.routing_table:
                    self._kademlia_node.routing_table.node_id = correct_node_id
                
                logger.info("✅ Node ID correction applied")
                return True
            else:
                logger.debug("Node ID is already correct, no correction needed")
                return True
