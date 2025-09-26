import asyncio
import time
import hashlib
from typing import Dict, Any, List, Tuple
from common.utils import get_logger, get_host_ip
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class EventBasedDHTPublisher:
    """Event-driven DHT publisher that responds to changes"""
    
    def __init__(self, config: InferenceConfig, metrics_callback):
        self.config = config
        self.metrics_callback = metrics_callback
        self.kademlia_node = None
        self.running = False
        
        # Pure event-driven state - NO PERIODIC UPDATES
        self.last_published_metrics = {}
        self.metrics_change_threshold = 0.15  # 15% change triggers update (increased threshold)
        self.significant_change_only = True  # Only update on significant changes
        self.last_significant_change = 0
        
        # Monitoring task
        self.monitor_task = None
        
        # Parse bootstrap nodes
        self.bootstrap_nodes = self._parse_bootstrap_nodes(config.bootstrap_nodes)
        
        # Initialize hardware fingerprint for consistency validation
        try:
            from common.hardware_fingerprint import HardwareFingerprint
            self.hardware_fingerprint = HardwareFingerprint()
            logger.info(f"Hardware fingerprint initialized: {self.hardware_fingerprint.get_fingerprint_summary()}")
        except Exception as e:
            logger.warning(f"Could not initialize hardware fingerprint: {e}")
            self.hardware_fingerprint = None
    
    def _parse_bootstrap_nodes(self, bootstrap_str: str) -> List[Tuple[str, int]]:
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
    
    def _validate_node_id_consistency(self) -> bool:
        """Validate that the current node ID is consistent with hardware"""
        if not self.hardware_fingerprint:
            logger.debug("Hardware fingerprint not available, skipping validation")
            return True
        
        try:
            expected_node_id = self.hardware_fingerprint.generate_node_id(self.config.port)
            is_consistent = self.config.node_id == expected_node_id
            
            if not is_consistent:
                logger.warning(f"Node ID inconsistency detected:")
                logger.warning(f"  Current: {self.config.node_id[:16]}...")
                logger.warning(f"  Expected: {expected_node_id[:16]}...")
                logger.warning("This may indicate hardware changes or configuration issues")
                
                # Check if we should update the stored node ID
                stored_node_id = self.config._get_stored_node_id() if hasattr(self.config, '_get_stored_node_id') else None
                if stored_node_id and stored_node_id != expected_node_id:
                    logger.info("Hardware appears to have changed, updating stored node ID")
                    if hasattr(self.config, '_store_node_id'):
                        self.config._store_node_id(expected_node_id)
                        logger.info(f"Updated stored node ID to: {expected_node_id[:16]}...")
            else:
                logger.debug("Node ID is consistent with current hardware")
            
            return is_consistent
        except Exception as e:
            logger.warning(f"Could not validate node ID consistency: {e}")
            return True  # Assume consistent if validation fails
    
    async def start(self):
        """Start the event-based publisher"""
        if self.running:
            return
        
        # Validate node ID consistency before starting
        consistency_check = self._validate_node_id_consistency()
        
        # If hardware changed, regenerate node ID
        if not consistency_check and self.hardware_fingerprint:
            try:
                new_node_id = self.hardware_fingerprint.generate_node_id(self.config.port)
                logger.warning(f"Hardware changed detected, updating node ID from {self.config.node_id[:16]}... to {new_node_id[:16]}...")
                
                # Update configuration
                old_node_id = self.config.node_id
                self.config.node_id = new_node_id
                
                # Store the new node ID
                if hasattr(self.config, '_store_node_id'):
                    self.config._store_node_id(new_node_id)
                
                logger.info(f"Node ID updated due to hardware changes: {old_node_id[:8]}... → {new_node_id[:8]}...")
                
            except Exception as e:
                logger.error(f"Failed to update node ID after hardware change: {e}")
                # Continue with existing node ID
        
        self.running = True
        
        # Use shared DHT service
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        
        try:
            self.kademlia_node = await dht_service.initialize(
                node_id=self.config.node_id,
                port=self.config.dht_port,
                bootstrap_nodes=self.bootstrap_nodes
            )
            
            # Validate that the DHT node is using the correct node ID
            if self.kademlia_node.node_id != self.config.node_id:
                logger.warning(f"DHT node ID mismatch: config={self.config.node_id[:16]}..., dht={self.kademlia_node.node_id[:16]}...")
                # Update DHT node to use config node ID
                self.kademlia_node.node_id = self.config.node_id
                logger.info("Updated DHT node to use hardware-based node ID")
                
        except Exception as e:
            logger.error(f"Failed to start DHT node: {e}")
            self.running = False
            raise
        
        # Start monitoring for changes
        self.monitor_task = asyncio.create_task(self._monitor_changes())
        
        # Publish initial state
        await self._publish_node_info()
        
        # Broadcast initial node join event
        try:
            await self._broadcast_node_event("node_joined", {
                'node_id': self.config.node_id,
                'ip': get_host_ip(),
                'port': self.config.port,
                'model': self.config.model_name,
                'dht_port': self.config.dht_port
            })
        except Exception as e:
            logger.debug(f"Could not broadcast initial node join: {e}")
        
        logger.info(f"Event-based DHT publisher started with hardware-based node ID: {self.config.node_id[:16]}...")
        
        # Log hardware fingerprint details for debugging
        if self.hardware_fingerprint:
            summary = self.hardware_fingerprint.get_fingerprint_summary()
            logger.info(f"Hardware fingerprint: {summary}")
    
    async def stop(self):
        """Stop the event-based publisher with enhanced departure broadcasting"""
        # Broadcast node leave event with retry mechanism
        departure_attempts = 3
        for attempt in range(departure_attempts):
            try:
                await self._broadcast_node_event("node_left", {
                    'node_id': self.config.node_id,
                    'ip': get_host_ip(),
                    'port': self.config.port,
                    'model': self.config.model_name,
                    'reason': 'graceful_shutdown',
                    'shutdown_attempt': attempt + 1
                })
                
                # Give time for the event to propagate
                await asyncio.sleep(1)
                break
                
            except Exception as e:
                logger.debug(f"Departure broadcast attempt {attempt + 1} failed: {e}")
                if attempt < departure_attempts - 1:
                    await asyncio.sleep(0.5)
        
        self.running = False
        
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        # Remove our node from DHT with retry
        for attempt in range(2):
            try:
                await self._unpublish_node_info()
                break
            except Exception as e:
                logger.error(f"Error unpublishing node info (attempt {attempt + 1}): {e}")
                if attempt == 0:
                    await asyncio.sleep(0.5)
        
        self.kademlia_node = None
        logger.info("Event-based DHT publisher stopped")
    
    async def _monitor_changes(self):
        """Enhanced monitoring with active departure detection"""
        hardware_check_interval = 600  # Check hardware every 10 minutes
        departure_check_interval = 45  # Check for departures every 45 seconds
        last_hardware_check = 0
        last_departure_check = 0
        
        while self.running:
            try:
                current_metrics = self.metrics_callback()
                current_time = time.time()
                
                # Only check if metrics changed significantly (NO PERIODIC UPDATES)
                should_update = self._should_update_metrics(current_metrics)
                
                # Hardware consistency check (less frequent)
                if current_time - last_hardware_check > hardware_check_interval:
                    await self.handle_hardware_change()
                    last_hardware_check = current_time
                
                # Active departure detection
                if current_time - last_departure_check > departure_check_interval:
                    await self._active_departure_detection()
                    last_departure_check = current_time
                
                # Only update on significant changes
                if should_update:
                    await self._publish_node_info()
                    
                    # Broadcast node update event
                    await self._broadcast_node_event("node_updated", {
                        'node_id': self.config.node_id,
                        'ip': get_host_ip(),
                        'port': self.config.port,
                        'model': self.config.model_name,
                        'metrics': current_metrics,
                        'change_reason': 'significant_metrics_change'
                    })
                    
                    self.last_published_metrics = current_metrics.copy()
                    self.last_significant_change = current_time
                    logger.info(f"Published update due to significant metric change")
                
                # Monitor for node departures in DHT
                await self._check_for_node_departures()
                
                # Much longer sleep - we're not polling, just checking for significant changes
                await asyncio.sleep(30)  # Reduced from 5 to 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring changes: {e}")
                await asyncio.sleep(60)  # Longer wait on errors
    
    def _should_update_metrics(self, current_metrics: Dict[str, Any]) -> bool:
        """Check if metrics changed enough to warrant an update"""
        if not self.last_published_metrics:
            return True
        
        # Check for significant changes in key metrics
        for key in ['load', 'tps']:
            if key in current_metrics and key in self.last_published_metrics:
                old_value = self.last_published_metrics[key]
                new_value = current_metrics[key]
                
                # Avoid division by zero
                if old_value == 0 and new_value == 0:
                    continue
                
                if old_value == 0:
                    change_ratio = 1.0  # Treat as 100% change
                else:
                    change_ratio = abs(new_value - old_value) / old_value
                
                if change_ratio > self.metrics_change_threshold:
                    logger.debug(f"Significant change in {key}: {old_value} -> {new_value} ({change_ratio:.2%})")
                    return True
        
        return False
    
    async def _publish_node_info(self):
        """Publish current node info to DHT"""
        try:
            # Get current metrics
            metrics = self.metrics_callback()
            
            # Discover all available IP addresses
            from common.network_utils import NetworkInterfaceDiscovery
            
            available_ips = NetworkInterfaceDiscovery.get_advertisable_ips(
                exclude_loopback=True,
                exclude_link_local=True,
                only_up_interfaces=True
            )
            
            # Get primary IP
            if available_ips:
                primary_ip = available_ips[0]
            else:
                from common.utils import get_host_ip
                primary_ip = get_host_ip()
                available_ips = [primary_ip]
            
            # Classify IP types
            ip_types = {}
            interfaces = NetworkInterfaceDiscovery.discover_all_interfaces()
            for interface in interfaces:
                if interface.ip in available_ips:
                    ip_types[interface.ip] = interface.classification
            
            node_info = {
                'node_id': self.config.node_id,
                'ip': primary_ip,
                'port': self.config.port,
                'model': self.config.model_name,
                'load': metrics['load'],
                'tps': metrics['tps'],
                'uptime': metrics['uptime'],
                'last_seen': int(time.time()),
                'dht_port': self.config.dht_port,
                'available_ips': available_ips,
                'ip_types': ip_types,
                'multi_ip_enabled': True,
                'hardware_based': True,  # Flag to indicate hardware-based node ID
                'hardware_fingerprint_version': '1.0',  # Version for future compatibility
            }
            
            # Add hardware fingerprint summary for debugging and validation
            if self.hardware_fingerprint:
                node_info['hardware_summary'] = self.hardware_fingerprint.get_fingerprint_summary()
                node_info['hardware_consistency'] = self.hardware_fingerprint.validate_consistency(
                    self.config.node_id, self.config.port
                )
            
            # Store under multiple keys for different discovery patterns
            keys = [
                f"model:{self.config.model_name}",  # Find by model
                f"node:{self.config.node_id}",      # Find specific node
                f"hardware:{self._get_hardware_key()}"  # Find by hardware fingerprint
            ]
            
            for key in keys:
                try:
                    success = await self.kademlia_node.store(key, node_info)
                    if success:
                        logger.debug(f"Published node info under key: {key}")
                    else:
                        logger.warning(f"Failed to publish under key: {key}")
                except Exception as e:
                    logger.error(f"Error publishing to key {key}: {e}")
            
            # Update all_nodes registry
            await self._update_all_nodes_registry(node_info)
        
            # Broadcast node update via SSE only on significant changes
            # if self._is_significant_update(node_info):
            #     await self._broadcast_node_event("node_updated", node_info)
        
            # logger.debug(f"Published hardware-based node info: load={metrics['load']:.3f}, tps={metrics['tps']:.2f}")
            
        except Exception as e:
            logger.error(f"Error publishing node info: {e}")

    async def _broadcast_node_event(self, event_type: str, node_info: Dict[str, Any]):
        """Broadcast node events via SSE with enhanced metadata"""
        try:
            # Import here to avoid circular imports
            from inference_node.server import sse_handler
            
            if sse_handler and hasattr(sse_handler, 'broadcast_event'):
                # Enhanced event data
                event_data = {
                    'node_info': node_info,
                    'timestamp': time.time(),
                    'event_driven': True,
                    'source': 'dht_publisher',
                    'event_id': f"{event_type}_{int(time.time() * 1000)}",
                    'network_size': len(getattr(self, '_last_known_nodes', set()))
                }
                
                await sse_handler.broadcast_event(event_type, event_data)
                logger.info(f"✅ Broadcasted {event_type} event: {node_info.get('node_id', 'unknown')[:12]}...")
            else:
                logger.debug(f"SSE handler not available for {event_type}")
        except Exception as e:
            logger.debug(f"SSE broadcast not available for {event_type}: {e}")
    
    async def _check_for_node_departures(self):
        """Check DHT for nodes that have left the network"""
        try:
            if not self.kademlia_node:
                return
            
            # Get current DHT contacts
            current_contacts = set()
            if self.kademlia_node.routing_table:
                contacts = self.kademlia_node.routing_table.get_all_contacts()
                current_contacts = {contact.node_id for contact in contacts}
            
            # Get published nodes
            published_nodes = set()
            try:
                all_nodes_data = await self.kademlia_node.find_value("all_nodes")
                if all_nodes_data:
                    if isinstance(all_nodes_data, list):
                        published_nodes = {node.get('node_id') for node in all_nodes_data if isinstance(node, dict) and node.get('node_id')}
                    elif isinstance(all_nodes_data, dict) and all_nodes_data.get('node_id'):
                        published_nodes = {all_nodes_data['node_id']}
            except Exception as e:
                logger.debug(f"Could not check published nodes: {e}")
            
            # Find nodes that were published but are no longer in DHT
            if hasattr(self, '_last_known_nodes'):
                departed_nodes = self._last_known_nodes - current_contacts - published_nodes
                
                for departed_node_id in departed_nodes:
                    if departed_node_id != self.config.node_id:  # Don't report ourselves as departed
                        logger.info(f"Detected node departure: {departed_node_id[:8]}...")
                        
                        # Broadcast node left event
                        await self._broadcast_node_event("node_left", {
                            'node_id': departed_node_id,
                            'reason': 'dht_removal',
                            'detected_by': self.config.node_id
                        })
            
            # Update known nodes
            self._last_known_nodes = current_contacts | published_nodes
            
        except Exception as e:
            logger.debug(f"Error checking for node departures: {e}")
    
    def _is_significant_update(self, node_info: Dict[str, Any]) -> bool:
        """Check if this update represents a significant change"""
        if not self.last_published_metrics:
            return True  # First update is always significant
        
        current_load = node_info.get('load', 0)
        current_tps = node_info.get('tps', 0)
        
        last_load = self.last_published_metrics.get('load', 0)
        last_tps = self.last_published_metrics.get('tps', 0)
        
        # Check for significant changes (15% threshold)
        load_change = abs(current_load - last_load) / max(last_load, 0.01)
        tps_change = abs(current_tps - last_tps) / max(last_tps, 0.01)
        
        return load_change > self.metrics_change_threshold or tps_change > self.metrics_change_threshold
    
    async def _update_all_nodes_registry(self, node_info):
        """Update the all_nodes registry with proper aggregation"""
        try:
            # Get existing all_nodes data
            existing_data = await self.kademlia_node.find_value("all_nodes")
            
            if existing_data is None:
                existing_data = []
            elif not isinstance(existing_data, list):
                existing_data = [existing_data]
            
            # Remove our old entry if it exists
            existing_data = [node for node in existing_data 
                            if node.get('node_id') != self.config.node_id]
            
            # Add our current info
            existing_data.append(node_info)
            
            # Store updated list
            success = await self.kademlia_node.store("all_nodes", existing_data)
            if success:
                logger.debug(f"Updated all_nodes registry with {len(existing_data)} nodes")
            else:
                logger.warning("Failed to update all_nodes registry")
            
        except Exception as e:
            logger.error(f"Error updating all_nodes registry: {e}")
    
    async def _unpublish_node_info(self):
        """Remove node info from DHT when shutting down"""
        try:
            # Get existing all_nodes data
            existing_data = await self.kademlia_node.find_value("all_nodes")
            
            if existing_data and isinstance(existing_data, list):
                # Remove our entry
                updated_data = [node for node in existing_data 
                               if node.get('node_id') != self.config.node_id]
                
                # Store updated list
                await self.kademlia_node.store("all_nodes", updated_data)
                logger.info(f"Removed node from all_nodes registry")
            
        except Exception as e:
            logger.error(f"Error unpublishing node info: {e}")
    
    def get_node_info(self) -> Dict[str, Any]:
        """Get current node information including hardware fingerprint"""
        info = {
            'node_id': self.config.node_id,
            'hardware_based': True,
            'dht_running': self.running,
            'bootstrap_nodes': self.bootstrap_nodes,
            'hardware_key': self._get_hardware_key(),
            'consistency_validated': True
        }
        
        if self.hardware_fingerprint:
            info['hardware_fingerprint'] = self.hardware_fingerprint.get_fingerprint_summary()
            info['consistency_validated'] = self.hardware_fingerprint.validate_consistency(
                self.config.node_id, self.config.port
            )
        
        return info
    
    def _get_hardware_key(self) -> str:
        """Generate a hardware-based DHT key for node discovery"""
        if not self.hardware_fingerprint:
            return f"hardware:unknown:{self.config.node_id[:8]}"
        
        try:
            summary = self.hardware_fingerprint.get_fingerprint_summary()
            
            # Create a stable hardware identifier
            hardware_parts = [
                str(summary.get('cpu_count', 0)),
                str(summary.get('memory_gb', 0)),
                summary.get('hostname', 'unknown')[:8],  # Truncate hostname
                str(summary.get('mac_count', 0))
            ]
            
            hardware_id = hashlib.sha1('|'.join(hardware_parts).encode()).hexdigest()[:12]
            return f"hardware:{hardware_id}"
            
        except Exception as e:
            logger.debug(f"Error generating hardware key: {e}")
            return f"hardware:fallback:{self.config.node_id[:8]}"
    
    async def handle_hardware_change(self):
        """Handle hardware changes detected during runtime"""
        if not self.hardware_fingerprint:
            logger.warning("Cannot handle hardware change: hardware fingerprint not available")
            return
        
        try:
            # Generate new node ID based on current hardware
            new_node_id = self.hardware_fingerprint.generate_node_id(self.config.port)
            
            if new_node_id != self.config.node_id:
                logger.warning(f"Hardware change detected during runtime!")
                logger.info(f"Current node ID: {self.config.node_id[:16]}...")
                logger.info(f"New node ID: {new_node_id[:16]}...")
                
                # Unpublish old node info
                await self._unpublish_node_info()
                
                # Update configuration
                old_node_id = self.config.node_id
                self.config.node_id = new_node_id
                
                # Update DHT node ID
                if self.kademlia_node:
                    self.kademlia_node.node_id = new_node_id
                
                # Store new node ID
                if hasattr(self.config, '_store_node_id'):
                    self.config._store_node_id(new_node_id)
                
                # Republish with new node ID
                await self._publish_node_info()
                
                logger.info(f"Successfully updated node ID due to hardware change: {old_node_id[:8]}... → {new_node_id[:8]}...")
                
            else:
                logger.debug("Hardware check passed: no changes detected")
                
        except Exception as e:
            logger.error(f"Error handling hardware change: {e}")
    
    async def force_hardware_revalidation(self):
        """Force a complete hardware revalidation and node ID update if needed"""
        logger.info("Forcing hardware revalidation...")
        
        if not self.hardware_fingerprint:
            logger.warning("Cannot revalidate: hardware fingerprint not available")
            return False
        
        try:
            # Reinitialize hardware fingerprint to get fresh data
            from common.hardware_fingerprint import HardwareFingerprint
            self.hardware_fingerprint = HardwareFingerprint()
            
            # Check consistency with fresh data
            await self.handle_hardware_change()
            
            logger.info("Hardware revalidation completed")
            return True
            
        except Exception as e:
            logger.error(f"Error during hardware revalidation: {e}")
            return False
    
    async def _active_departure_detection(self):
        """Actively detect departed nodes by health checking published nodes"""
        try:
            # Get all published nodes
            all_nodes_data = await self.kademlia_node.find_value("all_nodes")
            if not all_nodes_data:
                return
            
            if not isinstance(all_nodes_data, list):
                all_nodes_data = [all_nodes_data]
            
            current_time = time.time()
            departed_nodes = []
            
            for node_data in all_nodes_data:
                if not isinstance(node_data, dict) or not node_data.get('node_id'):
                    continue
                    
                node_id = node_data['node_id']
                
                # Skip ourselves
                if node_id == self.config.node_id:
                    continue
                
                # Check if node is stale (no updates for 2 minutes)
                last_seen = node_data.get('last_seen', 0)
                if current_time - last_seen > 120:
                    # Verify the node is actually down
                    is_down = await self._verify_node_down(node_data)
                    if is_down:
                        departed_nodes.append(node_data)
                        logger.info(f"Detected departed node: {node_id[:8]}... (last seen: {current_time - last_seen:.0f}s ago)")
            
            # Broadcast departure events and clean up
            for node_data in departed_nodes:
                await self._handle_node_departure(node_data)
                
        except Exception as e:
            logger.error(f"Error in active departure detection: {e}")

    async def _verify_node_down(self, node_data: Dict[str, Any]) -> bool:
        """Verify that a node is actually down by attempting to contact it"""
        try:
            import aiohttp
            
            ip = node_data.get('ip')
            port = node_data.get('port')
            
            if not ip or not port:
                return True  # Consider down if no contact info
            
            # Quick health check with short timeout
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"http://{ip}:{port}/health") as response:
                    if response.status == 200:
                        logger.debug(f"Node {node_data['node_id'][:8]}... is actually still alive")
                        return False  # Node is alive
                        
        except Exception as e:
            logger.debug(f"Health check failed for {node_data['node_id'][:8]}...: {e}")
        
        return True  # Consider down if health check fails

    async def _handle_node_departure(self, node_data: Dict[str, Any]):
        """Handle a detected node departure"""
        node_id = node_data['node_id']
        
        # Remove from all_nodes registry
        try:
            existing_data = await self.kademlia_node.find_value("all_nodes")
            if existing_data and isinstance(existing_data, list):
                updated_data = [node for node in existing_data 
                               if node.get('node_id') != node_id]
                await self.kademlia_node.store("all_nodes", updated_data)
                logger.info(f"Removed departed node {node_id[:8]}... from all_nodes registry")
        except Exception as e:
            logger.error(f"Error removing departed node from registry: {e}")
        
        # Broadcast departure event
        await self._broadcast_node_event("node_left", {
            'node_id': node_id,
            'ip': node_data.get('ip'),
            'port': node_data.get('port'),
            'model': node_data.get('model'),
            'reason': 'health_check_failed',
            'detected_by': self.config.node_id,
            'last_seen': node_data.get('last_seen')
        })

    async def force_update(self):
        """Force an immediate update of node info"""
        if self.running:
            await self._publish_node_info()
            self.last_forced_update = time.time()
            logger.info("Forced DHT update completed")
