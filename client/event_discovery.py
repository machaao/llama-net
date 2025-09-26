import asyncio
import time
from typing import List, Optional, Dict, Any, Callable, Set
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from dht.kademlia_node import KademliaNode
from common.models import NodeInfo
from common.utils import get_logger
from common.network_utils import NetworkUtils, SubnetFilter
from client.discovery import DiscoveryInterface

logger = get_logger(__name__)

class NodeEventType(Enum):
    NODE_JOINED = "node_joined"
    NODE_LEFT = "node_left"
    NODE_UPDATED = "node_updated"
    NETWORK_CHANGED = "network_changed"

@dataclass
class NodeEvent:
    event_type: NodeEventType
    node_info: Optional[NodeInfo]
    timestamp: float
    metadata: Dict[str, Any] = None

class NodeEventListener(ABC):
    """Abstract base class for node event listeners"""
    
    @abstractmethod
    async def on_node_event(self, event: NodeEvent):
        """Handle a node event"""
        pass

class EventBasedDHTDiscovery(DiscoveryInterface):
    """Event-driven DHT discovery with real-time notifications"""
    
    def __init__(self, bootstrap_nodes: str = "", dht_port: int = 8001,
                 enable_subnet_filtering: bool = True,
                 connectivity_test: bool = True,
                 allowed_subnets: List[str] = None,
                 blocked_subnets: List[str] = None):
        
        self.bootstrap_nodes = self._parse_bootstrap_nodes(bootstrap_nodes)
        self.dht_port = dht_port
        self.kademlia_node = None
        
        # Event-based state management
        self.active_nodes: Dict[str, NodeInfo] = {}  # node_id -> NodeInfo
        self.event_listeners: List[NodeEventListener] = []
        self.known_node_ids: Set[str] = set()
        
        # Network filtering configuration
        self.enable_subnet_filtering = enable_subnet_filtering
        self.connectivity_test = connectivity_test
        self.subnet_filter = SubnetFilter(
            allowed_subnets=allowed_subnets,
            blocked_subnets=blocked_subnets,
            auto_detect_local=enable_subnet_filtering
        ) if enable_subnet_filtering else None
        
        # Event processing
        self.event_queue = asyncio.Queue()
        self.event_processor_task = None
        self.dht_monitor_task = None
        self.running = False
        
        # Pure event-driven - NO POLLING
        self.last_dht_change = 0
        self.change_detection_threshold = 0.15  # 15% change threshold
        
        logger.info("Event-based DHT Discovery initialized")
    
    def _parse_bootstrap_nodes(self, bootstrap_str: str) -> List[tuple]:
        """Parse bootstrap nodes from comma-separated string"""
        if not bootstrap_str:
            return []
        
        nodes = []
        for node_str in bootstrap_str.split(','):
            try:
                ip, port = node_str.strip().split(':')
                nodes.append((ip, int(port)))
            except ValueError:
                logger.warning(f"Invalid bootstrap node format: {node_str}")
        
        return nodes
    
    async def start(self):
        """Start the event-based discovery service"""
        if self.running:
            return
        
        self.running = True
        
        # Initialize DHT connection
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        
        if dht_service.is_initialized():
            self.kademlia_node = dht_service.kademlia_node
            logger.info("Event discovery using existing shared DHT service")
        else:
            logger.warning("Shared DHT service not initialized")
            return
        
        # Start event processing
        self.event_processor_task = asyncio.create_task(self._process_events())
        self.dht_monitor_task = asyncio.create_task(self._monitor_dht_changes())
        
        # Initial network discovery
        await self._perform_initial_discovery()
        
        logger.info("Event-based discovery started")
    
    async def stop(self):
        """Stop the event-based discovery service"""
        self.running = False
        
        if self.event_processor_task:
            self.event_processor_task.cancel()
            try:
                await self.event_processor_task
            except asyncio.CancelledError:
                pass
        
        if self.dht_monitor_task:
            self.dht_monitor_task.cancel()
            try:
                await self.dht_monitor_task
            except asyncio.CancelledError:
                pass
        
        # Clear state
        self.active_nodes.clear()
        self.known_node_ids.clear()
        self.event_listeners.clear()
        
        logger.info("Event-based discovery stopped")
    
    def add_event_listener(self, listener: NodeEventListener):
        """Add an event listener for node changes"""
        self.event_listeners.append(listener)
        logger.debug(f"Added event listener: {type(listener).__name__}")
    
    def remove_event_listener(self, listener: NodeEventListener):
        """Remove an event listener"""
        if listener in self.event_listeners:
            self.event_listeners.remove(listener)
            logger.debug(f"Removed event listener: {type(listener).__name__}")
    
    async def _emit_event(self, event: NodeEvent):
        """Emit an event to all listeners"""
        await self.event_queue.put(event)
        
        # Also notify listeners directly for immediate processing
        for listener in self.event_listeners:
            try:
                if hasattr(listener, 'on_node_event'):
                    await listener.on_node_event(event)
                else:
                    # Handle function-based listeners
                    await listener(event)
            except Exception as e:
                logger.error(f"Error in direct event listener {type(listener).__name__}: {e}")
    
    async def _process_events(self):
        """Process events from the queue"""
        while self.running:
            try:
                # Wait for events with timeout to allow graceful shutdown
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
                
                # Update internal state
                await self._update_internal_state(event)
                
                # Notify all listeners
                for listener in self.event_listeners:
                    try:
                        await listener.on_node_event(event)
                    except Exception as e:
                        logger.error(f"Error in event listener {type(listener).__name__}: {e}")
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing event: {e}")
    
    async def _update_internal_state(self, event: NodeEvent):
        """Update internal node state based on events"""
        if not event.node_info:
            return
        
        node_id = event.node_info.node_id
        
        if event.event_type == NodeEventType.NODE_JOINED:
            self.active_nodes[node_id] = event.node_info
            self.known_node_ids.add(node_id)
            logger.info(f"🆕 Node joined: {node_id[:12]}... at {event.node_info.ip}:{event.node_info.port}")
            
        elif event.event_type == NodeEventType.NODE_LEFT:
            if node_id in self.active_nodes:
                del self.active_nodes[node_id]
                logger.info(f"👋 Node left: {node_id[:12]}...")
            
        elif event.event_type == NodeEventType.NODE_UPDATED:
            if node_id in self.active_nodes:
                self.active_nodes[node_id] = event.node_info
                logger.debug(f"🔄 Node updated: {node_id[:12]}...")
    
    async def _monitor_dht_changes(self):
        """Monitor DHT for real-time changes with active health checking"""
        last_routing_table_size = 0
        health_check_interval = 60  # Check node health every minute
        last_health_check = 0
        
        while self.running:
            try:
                current_time = time.time()
                
                # Check for routing table changes
                if self.kademlia_node and self.kademlia_node.routing_table:
                    current_size = len(self.kademlia_node.routing_table.get_all_contacts())
                    
                    if current_size != last_routing_table_size:
                        logger.debug(f"DHT routing table changed: {last_routing_table_size} -> {current_size}")
                        await self._handle_routing_table_change()
                        last_routing_table_size = current_size
                
                # Active health checking of known nodes
                if current_time - last_health_check > health_check_interval:
                    await self._health_check_active_nodes()
                    last_health_check = current_time
                
                # Pure event-driven monitoring - only respond to actual DHT changes
                await asyncio.sleep(30)  # Minimal sleep for graceful shutdown only
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring DHT changes: {e}")
                await asyncio.sleep(10)  # Wait longer on errors
    
    async def _handle_routing_table_change(self):
        """Handle changes in the DHT routing table"""
        try:
            # Get current contacts from routing table
            contacts = self.kademlia_node.routing_table.get_all_contacts()
            current_contact_ids = {contact.node_id for contact in contacts}
            
            # Find new contacts
            known_contact_ids = {node_id for node_id in self.known_node_ids}
            new_contact_ids = current_contact_ids - known_contact_ids
            
            # Process new contacts
            for contact in contacts:
                if contact.node_id in new_contact_ids:
                    await self._process_new_contact(contact)
            
            # Emit network change event
            await self._emit_event(NodeEvent(
                event_type=NodeEventType.NETWORK_CHANGED,
                node_info=None,
                timestamp=time.time(),
                metadata={"routing_table_size": len(contacts)}
            ))
            
        except Exception as e:
            logger.error(f"Error handling routing table change: {e}")
    
    async def _process_new_contact(self, contact):
        """Process a new DHT contact"""
        try:
            # Try to get node info via HTTP
            node_info = await self._get_node_info_from_contact(contact)
            
            if node_info:
                # Apply filtering
                if self._should_include_node(node_info):
                    await self._emit_event(NodeEvent(
                        event_type=NodeEventType.NODE_JOINED,
                        node_info=node_info,
                        timestamp=time.time()
                    ))
                else:
                    logger.debug(f"Node {contact.node_id[:8]}... filtered out")
            
        except Exception as e:
            logger.debug(f"Could not process contact {contact.node_id[:8]}...: {e}")
    
    async def _get_node_info_from_contact(self, contact) -> Optional[NodeInfo]:
        """Get detailed node info from a DHT contact"""
        # Try to find the HTTP port for this contact
        http_port = await self._probe_http_port(contact.ip, contact.node_id)
        if not http_port:
            return None
        
        # Get node information via HTTP
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://{contact.ip}:{http_port}/info") as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        
                        # Create NodeInfo from HTTP response
                        node_info = NodeInfo(
                            node_id=info.get('node_id', f"{contact.ip}:{http_port}"),
                            ip=contact.ip,
                            port=http_port,
                            model=info.get('model', 'unknown'),
                            load=info.get('load', 0.0),
                            tps=info.get('tps', 0.0),
                            uptime=info.get('uptime', 0),
                            last_seen=int(time.time())
                        )
                        
                        return node_info
        except Exception as e:
            logger.debug(f"Failed to get node info from {contact.ip}:{http_port}: {e}")
        
        return None
    
    async def _probe_http_port(self, ip: str, node_id: str) -> Optional[int]:
        """Probe for the correct HTTP port for a node"""
        import aiohttp
        
        # Try common HTTP ports
        test_ports = [8000, 8002, 8004, 8006, 8008, 8010]
        
        for port in test_ports:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                    async with session.get(f"http://{ip}:{port}/health") as resp:
                        if resp.status in [200, 404]:  # 404 is ok, means server is responding
                            return port
            except:
                continue
        
        return None
    
    def _should_include_node(self, node_info: NodeInfo) -> bool:
        """Check if a node should be included based on filtering rules"""
        if not self.enable_subnet_filtering:
            return True
        
        if self.subnet_filter:
            return self.subnet_filter.is_ip_allowed(node_info.ip)
        
        return True
    
    async def _perform_initial_discovery(self):
        """Perform initial discovery of existing nodes"""
        try:
            # Get published nodes from DHT storage
            published_nodes = await self._get_published_nodes()
            
            for node_data in published_nodes:
                if isinstance(node_data, dict) and node_data.get('node_id'):
                    # Check if node is still fresh - INCREASED THRESHOLD
                    if time.time() - node_data.get('last_seen', 0) < 180:  # Increased from 60 to 180
                        try:
                            node_info = NodeInfo(**node_data)
                            if self._should_include_node(node_info):
                                await self._emit_event(NodeEvent(
                                    event_type=NodeEventType.NODE_JOINED,
                                    node_info=node_info,
                                    timestamp=time.time()
                                ))
                        except Exception as e:
                            logger.debug(f"Failed to process published node: {e}")
            
            logger.info(f"Initial discovery completed: {len(self.active_nodes)} nodes")
            
        except Exception as e:
            logger.error(f"Error in initial discovery: {e}")
    
    async def _get_published_nodes(self) -> List[Dict[str, Any]]:
        """Get published nodes from DHT storage"""
        nodes = []
        
        try:
            # Try to get from all_nodes key
            node_data = await self.kademlia_node.find_value("all_nodes")
            if node_data:
                if isinstance(node_data, list):
                    nodes.extend(node_data)
                else:
                    nodes.append(node_data)
        except Exception as e:
            logger.debug(f"Error getting published nodes: {e}")
        
        return nodes
    
    async def _handle_significant_change_detection(self):
        """Handle significant changes in DHT state - event-driven only"""
        try:
            # Only check for changes when DHT routing table actually changes
            if not self.kademlia_node or not self.kademlia_node.routing_table:
                return
            
            current_contacts = self.kademlia_node.routing_table.get_all_contacts()
            current_time = time.time()
            
            # Only process if there's been a significant change in routing table
            if abs(len(current_contacts) - len(self.known_node_ids)) > 0:
                logger.info(f"Significant DHT change detected: {len(self.known_node_ids)} -> {len(current_contacts)} contacts")
                await self._handle_routing_table_change()
                self.last_dht_change = current_time
            
        except Exception as e:
            logger.error(f"Error in change detection: {e}")
    
    async def _handle_node_departure_event(self, node_id: str, reason: str = "dht_removal"):
        """Handle node departure events - only when explicitly detected"""
        if node_id in self.active_nodes:
            node_info = self.active_nodes[node_id]
            
            await self._emit_event(NodeEvent(
                event_type=NodeEventType.NODE_LEFT,
                node_info=node_info,
                timestamp=time.time(),
                metadata={"reason": reason, "event_driven": True}
            ))
            
            logger.info(f"Node departure event: {node_id[:8]}... (reason: {reason})")
    
    # DiscoveryInterface implementation
    async def get_nodes(self, model: Optional[str] = None, force_refresh: bool = False) -> List[NodeInfo]:
        """Get active nodes (real-time, no polling)"""
        nodes = list(self.active_nodes.values())
        
        # Filter by model if specified
        if model:
            nodes = [node for node in nodes if node.model == model]
        
        return nodes
    
    async def find_specific_node(self, node_id: str) -> Optional[NodeInfo]:
        """Find a specific node by ID"""
        return self.active_nodes.get(node_id)
    
    def configure_subnet_filtering(self, 
                                 enable: bool = True,
                                 connectivity_test: bool = True,
                                 allowed_subnets: List[str] = None,
                                 blocked_subnets: List[str] = None):
        """Update subnet filtering configuration"""
        self.enable_subnet_filtering = enable
        self.connectivity_test = connectivity_test
        
        if enable:
            self.subnet_filter = SubnetFilter(
                allowed_subnets=allowed_subnets,
                blocked_subnets=blocked_subnets,
                auto_detect_local=True
            )
            logger.info("Subnet filtering configuration updated")
        else:
            self.subnet_filter = None
            logger.info("Subnet filtering disabled")
        
        # Re-evaluate all current nodes
        asyncio.create_task(self._reevaluate_all_nodes())
    
    async def _verify_node_actually_down(self, node_info: NodeInfo) -> bool:
        """Verify that a node is actually down before emitting 'left' event"""
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(f"http://{node_info.ip}:{node_info.port}/health") as response:
                    if response.status == 200:
                        logger.info(f"Node {node_info.node_id[:8]}... is actually still alive, updating last_seen")
                        # Update the node's last_seen time
                        node_info.last_seen = int(time.time())
                        return False  # Node is alive
        except Exception as e:
            logger.debug(f"Health check failed for {node_info.node_id[:8]}...: {e}")
        
        return True  # Node is confirmed down
    
    async def _health_check_active_nodes(self):
        """Actively health check known nodes to detect departures"""
        if not self.active_nodes:
            return
        
        current_time = time.time()
        nodes_to_check = []
        
        # Find nodes that haven't been seen recently
        for node_id, node_info in self.active_nodes.items():
            time_since_seen = current_time - node_info.last_seen
            if time_since_seen > 90:  # Check nodes not seen for 90 seconds
                nodes_to_check.append((node_id, node_info))
        
        # Health check nodes in parallel
        if nodes_to_check:
            logger.debug(f"Health checking {len(nodes_to_check)} potentially stale nodes")
            
            tasks = []
            for node_id, node_info in nodes_to_check:
                task = asyncio.create_task(self._health_check_node(node_id, node_info))
                tasks.append(task)
            
            # Wait for all health checks to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            for i, result in enumerate(results):
                node_id, node_info = nodes_to_check[i]
                
                if isinstance(result, Exception):
                    logger.debug(f"Health check error for {node_id[:8]}...: {result}")
                    continue
                
                is_alive = result
                if not is_alive:
                    logger.info(f"Node {node_id[:8]}... failed health check, marking as departed")
                    await self._emit_event(NodeEvent(
                        event_type=NodeEventType.NODE_LEFT,
                        node_info=node_info,
                        timestamp=time.time(),
                        metadata={"reason": "health_check_failed", "last_seen": node_info.last_seen}
                    ))

    async def _health_check_node(self, node_id: str, node_info: NodeInfo) -> bool:
        """Health check a specific node"""
        try:
            import aiohttp
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(f"http://{node_info.ip}:{node_info.port}/health") as response:
                    if response.status == 200:
                        # Node is alive, update last_seen
                        node_info.last_seen = int(time.time())
                        logger.debug(f"Node {node_id[:8]}... health check passed")
                        return True
                        
        except Exception as e:
            logger.debug(f"Health check failed for {node_id[:8]}...: {e}")
        
        return False

    async def _reevaluate_all_nodes(self):
        """Re-evaluate all current nodes against new filtering rules"""
        nodes_to_remove = []
        
        for node_id, node_info in self.active_nodes.items():
            if not self._should_include_node(node_info):
                nodes_to_remove.append((node_id, node_info))
        
        for node_id, node_info in nodes_to_remove:
            await self._emit_event(NodeEvent(
                event_type=NodeEventType.NODE_LEFT,
                node_info=node_info,
                timestamp=time.time(),
                metadata={"reason": "filtering_rule_change"}
            ))
