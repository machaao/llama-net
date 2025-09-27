import asyncio
import time
import traceback
from typing import List, Optional, Dict, Any, Callable, Set
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from dht.kademlia_node import KademliaNode
from common.models import NodeInfo
from common.utils import get_logger
from common.network_utils import NetworkUtils, SubnetFilter
from common.validation_utils import NodeValidator, NetworkValidator
from client.discovery import DiscoveryInterface

logger = get_logger(__name__)

class EventMetrics:
    """Track event metrics and rates for monitoring"""
    
    def __init__(self):
        self.event_counts: Dict[str, int] = {}
        self.event_rates: Dict[str, List[float]] = {}
        self.last_event_times: Dict[str, float] = {}
        self.rate_window = 300  # 5 minutes
    
    def record_event(self, event_type: str):
        """Record an event occurrence"""
        current_time = time.time()
        
        # Update counts
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        self.last_event_times[event_type] = current_time
        
        # Update rate tracking
        if event_type not in self.event_rates:
            self.event_rates[event_type] = []
        
        # Add current timestamp and clean old ones
        self.event_rates[event_type].append(current_time)
        cutoff_time = current_time - self.rate_window
        self.event_rates[event_type] = [
            t for t in self.event_rates[event_type] if t > cutoff_time
        ]
    
    def get_event_rate(self, event_type: str, window: int = 60) -> float:
        """Get events per minute for the specified window"""
        if event_type not in self.event_rates:
            return 0.0
        
        current_time = time.time()
        cutoff_time = current_time - window
        recent_events = [
            t for t in self.event_rates[event_type] if t > cutoff_time
        ]
        
        return len(recent_events) * (60.0 / window)  # Events per minute
    
    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive event statistics"""
        current_time = time.time()
        stats = {
            "total_events": sum(self.event_counts.values()),
            "event_counts": self.event_counts.copy(),
            "event_rates_per_minute": {},
            "last_event_times": {}
        }
        
        for event_type in self.event_counts:
            stats["event_rates_per_minute"][event_type] = self.get_event_rate(event_type)
            if event_type in self.last_event_times:
                time_ago = current_time - self.last_event_times[event_type]
                stats["last_event_times"][event_type] = f"{time_ago:.1f}s ago"
        
        return stats

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
        
        # Event deduplication and metrics
        self.event_cache: Dict[str, float] = {}  # event_key -> timestamp
        self.cache_ttl = 30  # 30 seconds for deduplication
        self.event_metrics = EventMetrics()
        
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
        self.unknown_nodes_task = None
        self.cache_cleanup_task = None
        self.running = False
        
        # Pure event-driven - NO POLLING
        self.last_dht_change = 0
        self.change_detection_threshold = 0.15  # 15% change threshold
        
        logger.info("Event-based DHT Discovery initialized with deduplication")
    
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
        
        # Start background task to update unknown nodes
        self.unknown_nodes_task = asyncio.create_task(self._update_unknown_nodes())
        
        # Start cache cleanup task
        self.cache_cleanup_task = asyncio.create_task(self._cleanup_event_cache())
        
        # Initial network discovery
        await self._perform_initial_discovery()
        
        logger.info("Event-based discovery started with deduplication and background node updates")
    
    async def stop(self):
        """Stop the event-based discovery service"""
        self.running = False
        
        # Stop all tasks
        tasks = [self.event_processor_task, self.dht_monitor_task, self.unknown_nodes_task, self.cache_cleanup_task]
        for task in tasks:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Clear state
        self.active_nodes.clear()
        self.known_node_ids.clear()
        self.event_listeners.clear()
        self.event_cache.clear()
        
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
        """Emit an event to all listeners with deduplication and error handling"""
        try:
            # Create event key for deduplication
            event_key = self._create_event_key(event)
            current_time = time.time()
            
            # Check for duplicate events
            if event_key in self.event_cache:
                time_since_last = current_time - self.event_cache[event_key]
                if time_since_last < self.cache_ttl:
                    logger.debug(f"🔇 Skipping duplicate event: {event_key} (last seen {time_since_last:.1f}s ago)")
                    return
            
            # Update cache and record metrics
            self.event_cache[event_key] = current_time
            event_type_name = event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type)
            self.event_metrics.record_event(event_type_name)
            
            # ONLY add to event queue - let _process_events handle listener notification
            await self.event_queue.put(event)
            
            # Log the event emission
            node_id = event.node_info.node_id if event.node_info else "N/A"
            logger.info(f"📡 Queued {event_type_name} event for node {node_id}")
            
        except Exception as e:
            logger.error(f"Error emitting event {event.event_type}: {e}")
    
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
                        if hasattr(listener, 'on_node_event'):
                            await listener.on_node_event(event)
                        else:
                            # Handle function-based listeners
                            await listener(event)
                        logger.debug(f"✅ Notified listener {type(listener).__name__} of {event.event_type.value}")
                    except Exception as e:
                        logger.error(f"Error in event listener {type(listener).__name__}: {e}")
                
                # Log successful processing
                node_id = event.node_info.node_id if event.node_info else "N/A"
                logger.info(f"✅ Processed {event.event_type.value} event for node {node_id}")
                        
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
            logger.info(f"🆕 Node joined: {node_id} at {event.node_info.ip}:{event.node_info.port}")
            
        elif event.event_type == NodeEventType.NODE_LEFT:
            try:
                if node_id in self.active_nodes:
                    del self.active_nodes[node_id]

                self.known_node_ids.discard(node_id)
                await self.kademlia_node.handle_network_leave_event(node_id, 'interrupted')
                logger.info(f"👋 Node left: {node_id}")
            except Exception as e:
                traceback.print_exc()
                logger.error(e)

        elif event.event_type == NodeEventType.NODE_UPDATED:
            if node_id in self.active_nodes:
                self.active_nodes[node_id] = event.node_info
                logger.info(f"🔄 Node updated: {node_id}")
    
    async def _monitor_dht_changes(self):
        """Centralized DHT monitoring with event handling"""
        last_routing_table_size = 0
        health_check_interval = 15  # Check node health every 15 seconds
        last_health_check = 0
        event_sequence_number = 0
        
        while self.running:
            try:
                current_time = time.time()
                
                # Monitor routing table changes and emit events centrally
                if self.kademlia_node and self.kademlia_node.routing_table:
                    current_size = len(self.kademlia_node.routing_table.get_all_contacts())
                    
                    if current_size != last_routing_table_size:
                        event_sequence_number += 1
                        logger.info(f"DHT routing table changed: {last_routing_table_size} -> {current_size} (centralized)")
                        await self._handle_routing_table_change_centralized(event_sequence_number)
                        last_routing_table_size = current_size
                
                # Centralized health checking
                if current_time - last_health_check > health_check_interval:
                    await self._centralized_health_check()
                    last_health_check = current_time
                
                # Event-driven monitoring
                await asyncio.sleep(10)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in centralized DHT monitoring: {e}")
                await asyncio.sleep(10)
    
    async def _handle_routing_table_change_centralized(self, sequence_number: int):
        """Centralized handling of routing table changes"""
        try:
            # Get current contacts from routing table
            contacts = self.kademlia_node.routing_table.get_all_contacts()
            current_contact_ids = {contact.node_id for contact in contacts}
            
            # Find new contacts
            known_contact_ids = {node_id for node_id in self.known_node_ids}
            new_contact_ids = current_contact_ids - known_contact_ids
            
            logger.info(f"📊 Centralized DHT change (seq: {sequence_number}): {len(contacts)} total contacts, {len(new_contact_ids)} new")
            
            # Process new contacts with centralized event emission
            for contact in contacts:
                if contact.node_id in new_contact_ids:
                    logger.info(f"🆕 New DHT contact detected (centralized): {contact.node_id[:8]}... at {contact.ip}")
                    await self._process_new_contact_centralized(contact, sequence_number)
            
            # Update known contacts
            self.known_node_ids.update(current_contact_ids)
            
            # Emit centralized network change event
            await self._emit_event(NodeEvent(
                event_type=NodeEventType.NETWORK_CHANGED,
                node_info=None,
                timestamp=time.time(),
                metadata={
                    "routing_table_size": len(contacts),
                    "new_contacts": len(new_contact_ids),
                    "sequence_number": sequence_number,
                    "centralized_handling": True
                }
            ))
            
        except Exception as e:
            logger.error(f"Error in centralized routing table change handling: {e}")
    
    async def _process_new_contact_centralized(self, contact, sequence_number: int):
        """Process a new DHT contact with centralized event handling"""
        try:
            logger.debug(f"🔍 Processing new DHT contact (centralized): {contact.node_id[:8]}... at {contact.ip}")
            
            # Validate contact before processing
            if not self._validate_contact(contact):
                logger.warning(f"❌ Contact validation failed for {contact.node_id[:8]}...")
                return
            
            # Check if this is truly a new contact (not just an update)
            is_truly_new = contact.node_id not in self.known_node_ids
            
            # Try to get node info via HTTP with retry
            node_info = await self._get_node_info_from_contact_enhanced(contact)
            
            if node_info:
                # Apply filtering for internal tracking
                should_track = self._should_include_node_enhanced(node_info, contact)
                
                if should_track:
                    # Add to internal tracking first
                    self.active_nodes[node_info.node_id] = node_info
                    self.known_node_ids.add(node_info.node_id)
                    
                    # Emit centralized events
                    if is_truly_new:
                        await self._emit_event(NodeEvent(
                            event_type=NodeEventType.NODE_JOINED,
                            node_info=node_info,
                            timestamp=time.time(),
                            metadata={
                                "source": "centralized_discovery",
                                "discovery_method": "http_probe" if node_info.model != 'unknown' else "dht_fallback",
                                "contact_ip": contact.ip,
                                "contact_port": getattr(contact, 'port', None),
                                "sequence_number": sequence_number,
                                "centralized_handling": True
                            }
                        ))
                        logger.info(f"✅ Centralized NODE_JOINED event for {contact.node_id[:8]}...")
                    else:
                        # This is an update to existing node
                        await self._emit_event(NodeEvent(
                            event_type=NodeEventType.NODE_UPDATED,
                            node_info=node_info,
                            timestamp=time.time(),
                            metadata={
                                "source": "centralized_update",
                                "sequence_number": sequence_number,
                                "update_reason": "contact_refresh"
                            }
                        ))
                        logger.debug(f"✅ Centralized NODE_UPDATED event for {contact.node_id[:8]}...")
                else:
                    logger.debug(f"❌ Node {contact.node_id[:8]}... filtered out by inclusion rules")
                    # Still track that we've seen this node to avoid repeated processing
                    self.known_node_ids.add(contact.node_id)
            else:
                logger.warning(f"❌ Could not create node info for contact {contact.node_id[:8]}...")
                
        except Exception as e:
            logger.error(f"❌ Error processing centralized contact {contact.node_id[:8]}...: {e}")
    
    async def _get_node_info_from_contact_enhanced(self, contact) -> Optional[NodeInfo]:
        """Get detailed node info from a DHT contact with enhanced retry logic and verification"""
        # Try to find the HTTP port with enhanced probing (now includes node ID verification)
        http_port = await self._probe_http_port_enhanced(contact.ip, contact.node_id)
        
        if http_port:
            # Get node information via HTTP with retry
            for attempt in range(3):
                try:
                    import aiohttp
                    timeout = aiohttp.ClientTimeout(total=8, connect=3)
                    
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(f"http://{contact.ip}:{http_port}/info") as resp:
                            if resp.status == 200:
                                info = await resp.json()
                                
                                # CRITICAL: Verify node ID matches before creating NodeInfo
                                response_node_id = info.get('node_id')
                                if response_node_id != contact.node_id:
                                    logger.error(f"❌ CRITICAL: Node ID mismatch during info retrieval!")
                                    logger.error(f"  Expected: {contact.node_id[:8]}...")
                                    logger.error(f"  Got: {response_node_id[:8] if response_node_id else 'none'}...")
                                    logger.error(f"  This indicates port {http_port} belongs to a different node!")
                                    return None  # Reject this contact completely
                                
                                # Create NodeInfo with verified data
                                node_info = NodeInfo(
                                    node_id=contact.node_id,  # Use contact node_id as authoritative
                                    ip=contact.ip,
                                    port=http_port,
                                    model=info.get('model', 'unknown'),
                                    load=info.get('load', 0.0),
                                    tps=info.get('tps', 0.0),
                                    uptime=info.get('uptime', 0),
                                    last_seen=int(time.time()),
                                    event_driven=True,
                                    change_reason='verified_http_discovery'
                                )
                                
                                logger.info(f"✅ Got verified node info for {contact.node_id[:8]}... via HTTP on port {http_port} (attempt {attempt + 1})")
                                return node_info
                                
                except Exception as e:
                    logger.debug(f"HTTP attempt {attempt + 1} failed for {contact.ip}:{http_port}: {e}")
                    if attempt < 2:  # Retry with exponential backoff
                        await asyncio.sleep(2 ** attempt)
                    continue
        
        # STRICT: Don't create fallback nodes if we can't verify the port
        logger.warning(f"❌ Could not verify HTTP port for {contact.node_id[:8]}... at {contact.ip}")
        logger.warning(f"   Skipping fallback creation to prevent port misassociation")
        return None
    
    async def _probe_http_port_enhanced(self, ip: str, node_id: str) -> Optional[int]:
        """Enhanced HTTP port probing with node ID verification to prevent mismatched associations"""
        import aiohttp
        
        # Expanded port range with common LlamaNet patterns
        test_ports = [8000, 8002, 8004, 8006, 8008, 8010, 8012, 8014, 8016, 8018, 8020, 8022, 8024]
        
        # Test ports in parallel with limited concurrency
        semaphore = asyncio.Semaphore(5)  # Limit concurrent tests
        
        async def test_port_enhanced(port):
            async with semaphore:
                try:
                    timeout = aiohttp.ClientTimeout(total=4, connect=2)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        # FIRST: Try /info endpoint to verify node ID
                        try:
                            async with session.get(f"http://{ip}:{port}/info") as resp:
                                if resp.status == 200:
                                    info = await resp.json()
                                    response_node_id = info.get('node_id')
                                    if response_node_id == node_id:
                                        logger.debug(f"✅ Verified correct node {node_id[:8]}... on port {port}")
                                        return port
                                    else:
                                        logger.debug(f"❌ Wrong node on port {port}: expected {node_id[:8]}..., got {response_node_id[:8] if response_node_id else 'none'}...")
                                        return None
                        except Exception as e:
                            logger.debug(f"Node ID verification failed for {ip}:{port}: {e}")
                        
                        # FALLBACK: Try other endpoints but mark as unverified
                        endpoints = ['/health', '/status', '/v1/models', '/']
                        for endpoint in endpoints:
                            try:
                                async with session.get(f"http://{ip}:{port}{endpoint}") as resp:
                                    if resp.status in [200, 404, 405, 501]:  # Any valid HTTP response
                                        logger.warning(f"⚠️ Found HTTP service on port {port} but couldn't verify node ID via {endpoint} - potential mismatch!")
                                        # Don't return unverified ports to prevent misassociation
                                        return None
                            except:
                                continue
                except:
                    pass
                return None
        
        # Test all ports in parallel
        tasks = [test_port_enhanced(port) for port in test_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Return first verified port only
        for result in results:
            if isinstance(result, int):
                return result
        
        logger.debug(f"Enhanced probe could not find verified HTTP port for {node_id[:8]}... at {ip}")
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

    async def _update_active_nodes_from_data_with_events(self, nodes_data, previous_nodes):
        """Update active nodes from data with proper event emission"""
        new_active_nodes = {}
        
        for node_data in nodes_data:
            if isinstance(node_data, dict) and node_data.get('node_id'):
                try:
                    node_info = NodeInfo(**node_data)
                    if self._should_include_node(node_info):
                        node_id = node_info.node_id
                        new_active_nodes[node_id] = node_info
                        
                        # Check if this is a new node or an update
                        if node_id not in previous_nodes:
                            # New node discovered
                            await self._emit_event(NodeEvent(
                                event_type=NodeEventType.NODE_JOINED,
                                node_info=node_info,
                                timestamp=time.time(),
                                metadata={
                                    "source": "data_refresh",
                                    "discovery_method": "topology_refresh",
                                    "newly_discovered": True
                                }
                            ))
                            logger.info(f"✅ Emitted NODE_JOINED event for {node_id[:8]}... (discovered in data refresh)")
                        else:
                            # Check for significant updates
                            previous_node = previous_nodes[node_id]
                            if self._is_significant_node_update(previous_node, node_info):
                                await self._emit_event(NodeEvent(
                                    event_type=NodeEventType.NODE_UPDATED,
                                    node_info=node_info,
                                    timestamp=time.time(),
                                    metadata={
                                        "source": "data_refresh",
                                        "update_reason": "topology_refresh",
                                        "previous_last_seen": previous_node.last_seen
                                    }
                                ))
                                logger.debug(f"✅ Emitted NODE_UPDATED event for {node_id[:8]}... (updated in data refresh)")
                        
                        # Set status based on last_seen time
                        time_since_last_seen = (time.time() - node_info.last_seen)
                        if time_since_last_seen < 60:
                            self.nodeStatuses.set(node_id, 'online')
                            self.nodeLastEvent.set(node_id, time.time() * 1000)  # Convert to milliseconds
                            self.nodeEventTypes.set(node_id, 'topology_refresh')
                        else:
                            self.nodeStatuses.set(node_id, 'unknown')
                            
                except Exception as e:
                    logger.debug(f"Failed to process node data: {e}")
        
        # Update the active nodes
        self.active_nodes = new_active_nodes
        
        logger.info(f"📊 Updated activeNodes from data with events: {len(self.active_nodes)} nodes")

    def _is_significant_node_update(self, previous_node, current_node):
        """Check if a node update is significant enough to emit an event"""
        try:
            # Check for significant changes
            if abs(previous_node.load - current_node.load) > 0.1:
                return True
            if abs(previous_node.tps - current_node.tps) > 1.0:
                return True
            if previous_node.ip != current_node.ip or previous_node.port != current_node.port:
                return True
            if current_node.last_seen - previous_node.last_seen > 30:  # 30 seconds
                return True
            
            return False
        except Exception as e:
            logger.debug(f"Error checking node update significance: {e}")
            return False

    def _create_event_key(self, event: NodeEvent) -> str:
        """Create a unique key for event deduplication"""
        event_type = event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type)
        
        if event.node_info:
            # Include node-specific information for node events
            node_key = f"{event.node_info.node_id}:{event.node_info.ip}:{event.node_info.port}"
            return f"{event_type}:{node_key}"
        else:
            # For network-wide events, use just the event type
            return f"{event_type}:network"
    
    async def _cleanup_event_cache(self):
        """Periodically clean up old entries from event cache"""
        while self.running:
            try:
                current_time = time.time()
                expired_keys = []
                
                for event_key, timestamp in self.event_cache.items():
                    if current_time - timestamp > self.cache_ttl * 2:  # Clean up entries older than 2x TTL
                        expired_keys.append(event_key)
                
                for key in expired_keys:
                    del self.event_cache[key]
                
                if expired_keys:
                    logger.debug(f"🧹 Cleaned up {len(expired_keys)} expired event cache entries")
                
                # Clean up every 60 seconds
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in event cache cleanup: {e}")
                await asyncio.sleep(60)
    
    def get_event_stats(self):
        """Get statistics about event emission for debugging"""
        return {
            "active_nodes": len(self.active_nodes),
            "known_node_ids": len(self.known_node_ids),
            "event_listeners": len(self.event_listeners),
            "event_queue_size": self.event_queue.qsize() if hasattr(self.event_queue, 'qsize') else 0,
            "last_dht_change": self.last_dht_change,
            "running": self.running,
            "event_cache_size": len(self.event_cache),
            "cache_ttl": self.cache_ttl,
            "event_metrics": self.event_metrics.get_stats()
        }

    async def debug_emit_test_events(self):
        """Debug method to test event emission"""
        logger.info("🧪 Emitting test events for debugging...")
        
        # Test join event
        test_node = NodeInfo(
            node_id="test_node_" + str(int(time.time())),
            ip="127.0.0.1",
            port=8999,
            model="test_model",
            load=0.5,
            tps=10.0,
            uptime=100,
            last_seen=int(time.time())
        )
        
        await self._emit_event(NodeEvent(
            event_type=NodeEventType.NODE_JOINED,
            node_info=test_node,
            timestamp=time.time(),
            metadata={"source": "debug_test", "test": True}
        ))
        
        # Wait a bit then emit leave event
        await asyncio.sleep(1)
        
        await self._emit_event(NodeEvent(
            event_type=NodeEventType.NODE_LEFT,
            node_info=test_node,
            timestamp=time.time(),
            metadata={"source": "debug_test", "test": True, "reason": "test_cleanup"}
        ))
        
        logger.info("🧪 Test events emitted")
    
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
    
    async def _centralized_health_check(self):
        """Centralized health checking with event emission"""
        if not self.active_nodes:
            return
        
        current_time = time.time()
        nodes_to_check = []
        
        # Find nodes that need health checking
        for node_id, node_info in self.active_nodes.items():
            time_since_seen = current_time - node_info.last_seen
            
            # Check nodes not seen for 60 seconds
            if time_since_seen > 60:
                nodes_to_check.append((node_id, node_info))
        
        if nodes_to_check:
            logger.debug(f"Centralized health checking {len(nodes_to_check)} potentially stale nodes")
            
            # Health check with limited concurrency
            semaphore = asyncio.Semaphore(3)
            tasks = []
            
            for node_id, node_info in nodes_to_check:
                task = asyncio.create_task(self._health_check_node_centralized(node_id, node_info, semaphore))
                tasks.append(task)
            
            # Wait for all health checks
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results with centralized event handling
            for i, result in enumerate(results):
                node_id, node_info = nodes_to_check[i]
                
                if isinstance(result, Exception):
                    logger.debug(f"Centralized health check error for {node_id[:8]}...: {result}")
                    await self._handle_centralized_node_departure(node_id, node_info, "health_check_exception")
                    continue
                
                is_alive = result
                if not is_alive:
                    if await self._validate_node_departure(node_id, node_info):
                        await self._handle_centralized_node_departure(node_id, node_info, "centralized_health_check_failed")

    async def _handle_centralized_node_departure(self, node_id: str, node_info: NodeInfo, reason: str):
        """Handle confirmed node departure with centralized event emission"""
        logger.info(f"Node {node_id} confirmed departed via {reason} (centralized)")
        
        # Remove from active tracking
        if node_id in self.active_nodes:
            del self.active_nodes[node_id]
        
        # Emit centralized leave event
        await self._emit_event(NodeEvent(
            event_type=NodeEventType.NODE_LEFT,
            node_info=node_info,
            timestamp=time.time(),
            metadata={
                "reason": reason, 
                "last_seen": node_info.last_seen,
                "validation_method": "centralized_health_check",
                "graceful": False,
                "centralized_handling": True
            }
        ))
        logger.info(f"✅ Centralized NODE_LEFT event for {node_id}... (reason: {reason})")

    async def _health_check_node_centralized(self, node_id: str, node_info: NodeInfo, semaphore) -> bool:
        """Centralized health check with multiple validation methods"""
        async with semaphore:
            try:
                import aiohttp
                
                # Try multiple endpoints with different timeouts
                endpoints = [
                    ('/health', 5),    # Primary health endpoint
                    ('/info', 8),      # Secondary info endpoint  
                    ('/status', 8),    # Status endpoint
                    ('/v1/models', 10) # OpenAI endpoint
                ]
                
                for endpoint, timeout in endpoints:
                    try:
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                            async with session.get(f"http://{node_info.ip}:{node_info.port}{endpoint}") as response:
                                if response.status in [200, 404, 405]:
                                    # Node is responding, update last_seen
                                    node_info.last_seen = int(time.time())
                                    logger.debug(f"Centralized health check passed for {node_id[:8]}... via {endpoint}")
                                    return True
                    except Exception as e:
                        logger.debug(f"Centralized health check failed for {node_id[:8]}... on {endpoint}: {e}")
                        continue
                
                logger.debug(f"All centralized health check methods failed for {node_id[:8]}...")
                return False
                
            except Exception as e:
                logger.debug(f"Centralized health check error for {node_id[:8]}...: {e}")
                return False

    async def _update_unknown_nodes(self):
        """Background task to update nodes with unknown model info"""
        while self.running:
            try:
                # Find nodes with unknown models
                unknown_nodes = [
                    (node_id, node_info) for node_id, node_info in self.active_nodes.items()
                    if node_info.model == 'unknown'
                ]
                
                if unknown_nodes:
                    logger.debug(f"🔄 Attempting to update {len(unknown_nodes)} nodes with unknown info")
                    
                    for node_id, node_info in unknown_nodes:
                        try:
                            # Try to get updated info via HTTP
                            updated_info = await self._get_updated_node_info(node_info)
                            if updated_info and updated_info.model != 'unknown':
                                # Update the node info
                                self.active_nodes[node_id] = updated_info
                                
                                # Emit update event
                                await self._emit_event(NodeEvent(
                                    event_type=NodeEventType.NODE_UPDATED,
                                    node_info=updated_info,
                                    timestamp=time.time(),
                                    metadata={"reason": "http_info_discovered"}
                                ))
                                
                                logger.info(f"✅ Updated node info for {node_id[:8]}... - model: {updated_info.model}")
                        except Exception as e:
                            logger.debug(f"Could not update node {node_id[:8]}...: {e}")
                
                # Run every 30 seconds
                await asyncio.sleep(30)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in unknown nodes update task: {e}")
                await asyncio.sleep(60)

    async def _get_updated_node_info(self, node_info: NodeInfo) -> Optional[NodeInfo]:
        """Try to get updated node info via HTTP"""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://{node_info.ip}:{node_info.port}/info") as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        
                        # Update the node info with new data
                        updated_info = NodeInfo(
                            node_id=node_info.node_id,
                            ip=node_info.ip,
                            port=node_info.port,
                            model=info.get('model', node_info.model),
                            load=node_info.load,  # Keep current metrics
                            tps=node_info.tps,
                            uptime=node_info.uptime,
                            last_seen=int(time.time())
                        )
                        
                        return updated_info
        except Exception as e:
            logger.debug(f"Failed to get updated info for {node_info.node_id[:8]}...: {e}")
        
        return None

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
    
    async def refreshNetworkDataOnTopologyChange(self):
        """Enhanced network data refresh with proper event emission"""
        try:
            # When network topology changes, we need to refresh our node data
            # since individual node events might not capture all changes
            logger.info("🔄 Network topology change detected, refreshing data...")
            
            # Store previous state for comparison
            previous_nodes = dict(self.active_nodes)
            previous_node_ids = set(self.active_nodes.keys())
            
            # Get fresh data from DHT
            all_nodes_data = await self._get_published_nodes()
            
            if all_nodes_data:
                # Update activeNodes from fresh data with events
                await self._update_active_nodes_from_data_with_events(all_nodes_data, previous_nodes)
                
                # Check for nodes that disappeared
                current_node_ids = set(self.active_nodes.keys())
                disappeared_nodes = previous_node_ids - current_node_ids
                
                for node_id in disappeared_nodes:
                    if node_id in previous_nodes:
                        await self._emit_event(NodeEvent(
                            event_type=NodeEventType.NODE_LEFT,
                            node_info=previous_nodes[node_id],
                            timestamp=time.time(),
                            metadata={
                                "reason": "topology_change_missing",
                                "source": "network_refresh",
                                "graceful": False
                            }
                        ))
                        logger.info(f"✅ Emitted NODE_LEFT event for {node_id[:8]}... (disappeared in topology change)")
                
                logger.info(f"🔄 Network data refreshed: {len(self.active_nodes)} active nodes")
            else:
                logger.warning("⚠️ Could not refresh network data after topology change")
                
        except Exception as e:
            logger.error(f"Error refreshing network data on topology change: {e}")
    
    def _validate_contact(self, contact) -> bool:
        """Use centralized validation"""
        return NodeValidator.validate_contact(contact)

    async def _estimate_http_port(self, contact) -> int:
        """Enhanced HTTP port estimation based on DHT port and common patterns"""
        dht_port = getattr(contact, 'port', 8001)
        
        # Common LlamaNet port patterns
        port_patterns = [
            dht_port - 1,      # DHT 8001 -> HTTP 8000
            dht_port + 1000,   # DHT 8001 -> HTTP 9001
            8000,              # Default HTTP port
            8002,              # Common alternative
            8004,              # Common alternative
            dht_port           # Same port (rare but possible)
        ]
        
        # Remove duplicates while preserving order
        unique_ports = []
        seen = set()
        for port in port_patterns:
            if port not in seen and 1024 <= port <= 65535:
                unique_ports.append(port)
                seen.add(port)
        
        return unique_ports[0] if unique_ports else 8000

    def _should_include_node_enhanced(self, node_info: NodeInfo, contact) -> bool:
        """Enhanced node inclusion check with additional validation and improved duplicate handling"""
        # Basic filtering
        if not self._should_include_node(node_info):
            return False
        
        # Additional validation for enhanced discovery
        try:
            # Validate node ID consistency
            if node_info.node_id != contact.node_id:
                logger.warning(f"Node ID inconsistency detected: {node_info.node_id[:8]}... vs {contact.node_id[:8]}...")
                return False
            
            # Check for reasonable port ranges
            if not (1024 <= node_info.port <= 65535):
                logger.warning(f"Invalid port range for node {node_info.node_id[:8]}...: {node_info.port}")
                return False
            
            # IMPROVED: Check for duplicate IP:port combinations but handle probe errors gracefully
            for existing_node in self.active_nodes.values():
                if (existing_node.ip == node_info.ip and 
                    existing_node.port == node_info.port and 
                    existing_node.node_id != node_info.node_id):
                    logger.warning(f"Duplicate IP:port detected: {node_info.ip}:{node_info.port}")
                    logger.warning(f"  Existing node: {existing_node.node_id[:8]}...")
                    logger.warning(f"  New node: {node_info.node_id[:8]}...")
                    
                    # Check if this might be a port probing error
                    if node_info.model == 'unknown' or existing_node.model == 'unknown':
                        logger.info(f"Possible port probe mismatch - rejecting unverified node {node_info.node_id[:8]}...")
                        return False
                    else:
                        logger.warning(f"Both nodes have model info - this indicates a real conflict")
                        # Prefer the node with more recent activity
                        if node_info.last_seen > existing_node.last_seen:
                            logger.info(f"New node {node_info.node_id[:8]}... is more recent, allowing")
                            # Remove the older conflicting node
                            if existing_node.node_id in self.active_nodes:
                                del self.active_nodes[existing_node.node_id]
                                logger.info(f"Removed older conflicting node {existing_node.node_id[:8]}...")
                        else:
                            logger.info(f"Existing node {existing_node.node_id[:8]}... is more recent, rejecting new")
                            return False
            
            return True
            
        except Exception as e:
            logger.debug(f"Enhanced inclusion check error: {e}")
            return False

    async def _validate_node_departure(self, node_id: str, node_info: NodeInfo) -> bool:
        """Validate that a node has actually departed before emitting leave event"""
        try:
            # Double-check with a final health probe
            import aiohttp
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                # Try the most reliable endpoint
                async with session.get(f"http://{node_info.ip}:{node_info.port}/health") as response:
                    if response.status == 200:
                        logger.info(f"Node {node_id[:8]}... is actually still alive during departure validation")
                        # Update last_seen and don't emit departure
                        node_info.last_seen = int(time.time())
                        return False
                        
        except Exception as e:
            logger.debug(f"Departure validation failed for {node_id[:8]}...: {e}")
        
        # Also check if node is still in DHT routing table
        if self.kademlia_node and self.kademlia_node.routing_table:
            contacts = self.kademlia_node.routing_table.get_all_contacts()
            for contact in contacts:
                if contact.node_id == node_id:
                    # Node is still in routing table, check if recently seen
                    if time.time() - contact.last_seen < 30:
                        logger.info(f"Node {node_id[:8]}... still active in DHT routing table")
                        return False
        
        logger.info(f"Node departure validated for {node_id}")
        return True
