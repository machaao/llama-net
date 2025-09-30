import asyncio
import time
import hashlib
import uuid
from typing import Dict, Any, List, Tuple, Optional
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
        """Start the event-based publisher with delayed join event"""
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

        # Initialize known nodes tracking for periodic detection
        self._known_node_ids = set()
        self._has_published_before = False
        self._join_event_sent = False  # NEW: Track if join event was sent
        
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
        
        # Publish initial state WITHOUT join event
        await self._publish_node_info_without_join()

        # Note: Join event will be sent post-uvicorn initialization
        # No longer registering callback here to avoid premature join event

        logger.info(f"Event-based DHT publisher started (join event delayed): {self.config.node_id[:16]}...")
        
        # Log hardware fingerprint details for debugging
        if self.hardware_fingerprint:
            summary = self.hardware_fingerprint.get_fingerprint_summary()
            logger.info(f"Hardware fingerprint: {summary}")
    
    async def stop(self):
        """Stop the event-based publisher with enhanced departure broadcasting and waiting"""
        logger.info("Stopping event-based DHT publisher...")
        
        # Prepare comprehensive departure info
        departure_info = {
            'node_id': self.config.node_id,
            'ip': get_host_ip(),
            'port': self.config.port,
            'model': self.config.model_name,
            'reason': 'graceful_shutdown',
            'last_seen': int(time.time()),
            'departure_timestamp': time.time(),
            'graceful': True,
            'final_metrics': self.metrics_callback() if self.metrics_callback else {}
        }
        
        # Set running to False immediately to stop monitoring
        self.running = False
        
        # Send departure events via multiple channels with retries
        departure_success = False
        max_attempts = 3
        
        for attempt in range(max_attempts):
            try:
                logger.info(f"📤 Sending departure notifications (attempt {attempt + 1}/{max_attempts})...")
                
                # Send via SSE (existing)
                sse_task = asyncio.create_task(
                    self._broadcast_node_event("node_left", departure_info)
                )
                
                # Send via DHT (enhanced)
                dht_task = asyncio.create_task(
                    self._publish_node_left_to_dht(departure_info)
                )
                
                # Send direct notifications to known contacts
                contacts_task = asyncio.create_task(
                    self._send_departure_to_contacts(departure_info)
                )
                
                # Wait for all departure notifications with timeout
                await asyncio.wait_for(
                    asyncio.gather(sse_task, dht_task, contacts_task, return_exceptions=True),
                    timeout=3.0
                )
                
                departure_success = True
                logger.info("✅ Departure notifications sent successfully")
                break
                
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Departure notification attempt {attempt + 1} timed out")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.0)  # Brief wait before retry
            except Exception as e:
                logger.warning(f"❌ Departure notification attempt {attempt + 1} failed: {e}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.0)
        
        if not departure_success:
            logger.error("❌ Failed to send departure notifications after all attempts")
        
        # Wait additional time for event propagation
        logger.info("⏳ Waiting for event propagation...")
        await asyncio.sleep(2.0)
        
        # Cancel monitoring tasks quickly
        tasks_to_cancel = [self.monitor_task]
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
        
        # Wait briefly for task cancellation
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[t for t in tasks_to_cancel if t], return_exceptions=True),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                logger.warning("⏰ Task cancellation timed out")
        
        # Quick unpublish attempt
        try:
            await asyncio.wait_for(self._unpublish_node_info(), timeout=1.0)
            logger.debug("✅ Node info unpublished")
        except asyncio.TimeoutError:
            logger.warning("⏰ Unpublish timed out during shutdown")
        except Exception as e:
            logger.debug(f"Unpublish failed during shutdown: {e}")
        
        self.kademlia_node = None
        logger.info("Event-based DHT publisher stopped")
    
    async def _monitor_changes(self):
        """Enhanced monitoring with periodic new node detection"""
        hardware_check_interval = 600  # Check hardware every 10 minutes
        departure_check_interval = 45  # Check for departures every 45 seconds
        discovery_scan_interval = 90   # Enhanced discovery every 90 seconds
        recovery_check_interval = 120  # Event recovery every 2 minutes
        
        last_hardware_check = 0
        last_departure_check = 0
        last_discovery_scan = 0
        last_recovery_check = 0

        # Initialize tracking variables
        if not hasattr(self, '_last_known_nodes'):
            self._last_known_nodes = set()
        if not hasattr(self, '_last_contact_time'):
            self._last_contact_time = {}
        if not hasattr(self, '_node_info_cache'):
            self._node_info_cache = {}

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
                    await self._proactive_departure_detection()
                    last_departure_check = current_time

                # Enhanced node discovery
                if current_time - last_discovery_scan > discovery_scan_interval:
                    await self._enhanced_node_discovery()
                    last_discovery_scan = current_time

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
                    logger.debug(f"Published update due to significant metric change")

                # Monitor for node departures in DHT
                await self._check_for_node_departures()

                # Sleep for 30 seconds between checks
                await asyncio.sleep(30)

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
    
    async def _publish_node_info_without_join(self):
        """Publish node info without sending join event"""
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

            # Get model detection info
            model_type = 'unknown'
            model_format = 'unknown'
            detection_confidence = 0.0

            if hasattr(self.config, 'get_model_detection_info'):
                detection_info = self.config.get_model_detection_info()
                model_type = detection_info.get('detected_type', 'unknown')
                model_format = detection_info.get('detected_format', 'unknown')
                detection_confidence = detection_info.get('confidence', 0.0)

            # Determine node capabilities
            capabilities = self._get_node_capabilities()

            node_info = {
                'node_id': self.config.node_id,
                'ip': primary_ip,
                'port': self.config.port,
                'model': self.config.model_name,
                'model_type': model_type,
                'model_format': model_format,
                'detection_confidence': detection_confidence,
                'load': metrics['load'],
                'tps': metrics['tps'],
                'uptime': metrics['uptime'],
                'last_seen': int(time.time()),
                'dht_port': self.config.dht_port,
                'available_ips': available_ips,
                'ip_types': ip_types,
                'multi_ip_enabled': True,
                'hardware_based': True,
                'hardware_fingerprint_version': '1.0',
                'is_first_publish': False,  # Don't trigger join event yet
                'join_pending': True,  # Indicate join is pending
                'capabilities': capabilities
            }

            # Add hardware fingerprint summary for debugging and validation
            if self.hardware_fingerprint:
                node_info['hardware_summary'] = self.hardware_fingerprint.get_fingerprint_summary()
                node_info['hardware_consistency'] = self.hardware_fingerprint.validate_consistency(
                    self.config.node_id, self.config.port
                )

            # Store under multiple keys for different discovery patterns
            keys = [
                f"model:{self.config.model_name}",
                f"node:{self.config.node_id}",
                f"hardware:{self._get_hardware_key()}"
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

            logger.info(f"Node info published (join event pending): {self.config.node_id[:12]}...")

        except Exception as e:
            logger.error(f"Error publishing node info: {e}")

    async def _publish_node_info(self):
        """Publish current node info to DHT (updates only, no join events)"""
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

            # Get model detection info
            model_type = 'unknown'
            model_format = 'unknown'
            detection_confidence = 0.0

            if hasattr(self.config, 'get_model_detection_info'):
                detection_info = self.config.get_model_detection_info()
                model_type = detection_info.get('detected_type', 'unknown')
                model_format = detection_info.get('detected_format', 'unknown')
                detection_confidence = detection_info.get('confidence', 0.0)

            # Determine node capabilities
            capabilities = self._get_node_capabilities()

            node_info = {
                'node_id': self.config.node_id,
                'ip': primary_ip,
                'port': self.config.port,
                'model': self.config.model_name,
                'model_type': model_type,
                'model_format': model_format,
                'detection_confidence': detection_confidence,
                'load': metrics['load'],
                'tps': metrics['tps'],
                'uptime': metrics['uptime'],
                'last_seen': int(time.time()),
                'dht_port': self.config.dht_port,
                'available_ips': available_ips,
                'ip_types': ip_types,
                'multi_ip_enabled': True,
                'hardware_based': True,
                'hardware_fingerprint_version': '1.0',
                'join_pending': False,
                'capabilities': capabilities
            }

            # Add hardware fingerprint summary for debugging and validation
            if self.hardware_fingerprint:
                node_info['hardware_summary'] = self.hardware_fingerprint.get_fingerprint_summary()
                node_info['hardware_consistency'] = self.hardware_fingerprint.validate_consistency(
                    self.config.node_id, self.config.port
                )

            # Store under multiple keys for different discovery patterns
            keys = [
                f"model:{self.config.model_name}",
                f"node:{self.config.node_id}",
                f"hardware:{self._get_hardware_key()}"
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

            # Mark that we've published before
            self._has_published_before = True

        except Exception as e:
            logger.error(f"Error publishing node info: {e}")

    async def _send_delayed_join_event(self):
        """Send the DHT join event after all services are ready"""
        if self._join_event_sent:
            return
        
        try:
            from common.utils import get_host_ip
            
            # Record join timestamp
            join_timestamp = time.time()
            self._join_timestamp = join_timestamp
            
            # Get current network info
            primary_ip = get_host_ip()
            available_ips = [primary_ip]
            
            try:
                from common.network_utils import NetworkInterfaceDiscovery
                available_ips = NetworkInterfaceDiscovery.get_advertisable_ips(
                    exclude_loopback=True,
                    exclude_link_local=True,
                    only_up_interfaces=True
                )
                if available_ips:
                    primary_ip = available_ips[0]
            except:
                pass

            # Send the delayed join event
            await self._broadcast_node_event("node_joined", {
                'node_id': self.config.node_id,
                'ip': primary_ip,
                'port': self.config.port,
                'model': self.config.model_name,
                'dht_port': self.config.dht_port,
                'available_ips': available_ips,
                'join_reason': 'post_uvicorn_ready',
                'timestamp': join_timestamp,
                'delayed_join': True,
                'services_ready': True,
                'uvicorn_ready': True
            })
            
            self._join_event_sent = True
            self._has_published_before = True
            
            logger.info(f"🎉 DHT join event sent post-uvicorn: {self.config.node_id[:12]}... (timestamp: {join_timestamp})")
            
            # Update node info to remove join_pending flag
            await self._update_node_info_post_join()
            
        except Exception as e:
            logger.error(f"Error sending delayed join event: {e}")

    async def send_post_uvicorn_join_event(self):
        """Send the DHT join event after uvicorn is fully initialized"""
        if self._join_event_sent:
            logger.debug("Join event already sent, skipping")
            return
        
        if not self.running or not self.kademlia_node:
            logger.warning("Cannot send join event - DHT not running")
            return
        
        try:
            logger.info("Sending post-uvicorn DHT join event...")
            await self._send_delayed_join_event()
            logger.info("✅ Post-uvicorn DHT join event sent successfully")
        except Exception as e:
            logger.error(f"Error sending post-uvicorn join event: {e}")
            raise

    async def _update_node_info_post_join(self):
        """Update node info after join event is sent"""
        try:
            # Republish with updated flags
            await self._publish_node_info()
        except Exception as e:
            logger.error(f"Error updating node info post-join: {e}")

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
    
    async def _publish_node_left_to_dht(self, node_info: Dict[str, Any]):
        """Publish node_left event to DHT network"""
        try:
            if not self.kademlia_node:
                logger.warning("Cannot publish node_left to DHT: Kademlia node not available")
                return
            
            # Create node_left event data with consistent naming
            node_left_event = {
                'event_type': 'node_left',  # ✅ Ensure this is "node_left"
                'node_id': node_info.get('node_id'),
                'ip': node_info.get('ip'),
                'port': node_info.get('port'),
                'model': node_info.get('model'),
                'reason': node_info.get('reason', 'unknown'),
                'timestamp': int(time.time()),
                'detected_by': self.config.node_id,
                'last_seen': node_info.get('last_seen'),
                'graceful': node_info.get('graceful', True),  # Add graceful shutdown flag
                'departure_timestamp': node_info.get('departure_timestamp', time.time())
            }
            
            # Store under multiple keys for better discovery
            keys = [
                f"events:node_left:{node_info.get('node_id', 'unknown')}",
                f"departures:{int(time.time())}:{node_info.get('node_id', 'unknown')[:8]}"
            ]
            
            for key in keys:
                try:
                    success = await self.kademlia_node.store(key, node_left_event)
                    if success:
                        logger.info(f"📡 Published node_left event to DHT under key: {key}")
                    else:
                        logger.warning(f"Failed to publish node_left to DHT under key: {key}")
                except Exception as e:
                    logger.error(f"Error publishing node_left to DHT key {key}: {e}")
                    
            # Also update the all_nodes registry to remove the departed node
            await self._remove_from_all_nodes_registry(node_info.get('node_id'))
            
        except Exception as e:
            logger.error(f"Error publishing node_left to DHT: {e}")

    async def _send_departure_to_contacts(self, departure_info: Dict[str, Any]):
        """Send departure notifications directly to known DHT contacts"""
        try:
            if not self.kademlia_node or not self.kademlia_node.routing_table:
                return
            
            contacts = self.kademlia_node.routing_table.get_all_contacts()
            if not contacts:
                logger.debug("No DHT contacts to notify of departure")
                return
            
            # Send to up to 5 most recent contacts
            recent_contacts = sorted(contacts, key=lambda c: c.last_seen, reverse=True)[:5]
            
            departure_message = {
                'type': 'departure_notification',
                'id': str(uuid.uuid4()),
                'sender_id': self.config.node_id,
                'departure_data': {
                    'departed_node_id': self.config.node_id,
                    'timestamp': time.time(),
                    'departure_reason': 'graceful_shutdown',
                    'notifier_node_id': self.config.node_id,
                    'node_info': departure_info
                }
            }
            
            notification_tasks = []
            for contact in recent_contacts:
                try:
                    task = asyncio.create_task(
                        self.kademlia_node.protocol.send_request(
                            departure_message, 
                            (contact.ip, contact.port)
                        )
                    )
                    notification_tasks.append(task)
                except Exception as e:
                    logger.debug(f"Failed to create departure notification task for {contact.node_id[:8]}...: {e}")
            
            if notification_tasks:
                # Wait for notifications with timeout
                results = await asyncio.wait_for(
                    asyncio.gather(*notification_tasks, return_exceptions=True),
                    timeout=2.0
                )
                
                success_count = sum(1 for result in results if not isinstance(result, Exception))
                logger.info(f"📤 Sent departure notifications to {success_count}/{len(notification_tasks)} contacts")
            
        except Exception as e:
            logger.error(f"Error sending departure notifications to contacts: {e}")

    async def _remove_from_all_nodes_registry(self, departed_node_id: str):
        """Remove departed node from all_nodes registry in DHT"""
        try:
            # Get existing all_nodes data
            existing_data = await self.kademlia_node.find_value("all_nodes")
            
            if existing_data and isinstance(existing_data, list):
                # Remove the departed node
                updated_data = [node for node in existing_data 
                               if node.get('node_id') != departed_node_id]
                
                # Store updated list
                success = await self.kademlia_node.store("all_nodes", updated_data)
                if success:
                    logger.info(f"Removed departed node {departed_node_id[:8]}... from DHT all_nodes registry")
                else:
                    logger.warning(f"Failed to remove departed node from DHT registry")
            
        except Exception as e:
            logger.error(f"Error removing departed node from DHT registry: {e}")
    
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
            'consistency_validated': True,
            'capabilities': self._get_node_capabilities()
        }
        
        if self.hardware_fingerprint:
            info['hardware_fingerprint'] = self.hardware_fingerprint.get_fingerprint_summary()
            info['consistency_validated'] = self.hardware_fingerprint.validate_consistency(
                self.config.node_id, self.config.port
            )
        
        return info
    
    def _get_node_capabilities(self) -> Dict[str, Any]:
        """Determine node capabilities based on detected model type and initialized services"""
        capabilities = {}
        
        # Check for LLM capabilities based on detection
        if hasattr(self.config, 'is_llm_model') and self.config.is_llm_model():
            capabilities['text_generation'] = True
            capabilities['chat_completion'] = True
            capabilities['model_type'] = 'llm'
        
        # Check for SD capabilities based on detection
        if hasattr(self.config, 'is_sd_model') and self.config.is_sd_model():
            capabilities['image_generation'] = True
            capabilities['model_type'] = 'sd'
            
            # Add SD-specific info if available
            try:
                from inference_node.sd_config import SDConfig
                if hasattr(self.config, 'model_path'):
                    sd_config = SDConfig(model_path=self.config.model_path)
                    capabilities['sd_model_type'] = sd_config.model_type
                    capabilities['sd_model_name'] = sd_config.model_name
            except:
                pass
        
        # Add detection metadata
        if hasattr(self.config, 'get_model_detection_info'):
            detection_info = self.config.get_model_detection_info()
            capabilities['detection'] = {
                'auto_detected': True,
                'confidence': detection_info.get('confidence', 0.0),
                'format': detection_info.get('detected_format', 'unknown')
            }
        
        return capabilities
    
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
    
    async def handle_join_notification(self, node_id: str, ip: str, port: int):
        """Handle join notifications from DHT protocol and broadcast via SSE"""
        try:
            # Don't broadcast our own joins
            if node_id == self.config.node_id:
                logger.debug(f"Ignoring own join notification: {node_id[:8]}...")
                return
            
            logger.info(f"🎉 Processing join notification from DHT: {node_id[:8]}... at {ip}:{port}")
            
            # Create basic node info from join notification
            node_info = {
                'node_id': node_id,
                'ip': ip,
                'port': port,
                'model': 'unknown',  # Will be enriched
                'load': 0.0,
                'tps': 0.0,
                'uptime': 0,
                'last_seen': int(time.time()),
                'join_source': 'dht_notification',
                'newly_discovered': True
            }
            
            # Broadcast join event via SSE
            await self._broadcast_node_event("node_joined", node_info)
            
            # Try to enrich node info asynchronously
            asyncio.create_task(self._enrich_joined_node_info(node_id, ip, port))
            
            logger.info(f"✅ Broadcasted join event for {node_id[:8]}... via SSE")
            
        except Exception as e:
            logger.error(f"Error handling join notification for {node_id[:8]}...: {e}")
    
    async def _enrich_joined_node_info(self, node_id: str, ip: str, port: int):
        """Enrich node info after join notification"""
        try:
            import aiohttp
            
            # Wait a moment for the node to be fully ready
            await asyncio.sleep(2)
            
            # Try to get detailed node info
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                try:
                    async with session.get(f"http://{ip}:{port}/info") as response:
                        if response.status == 200:
                            node_info = await response.json()
                            
                            # Create enriched node info
                            enriched_info = {
                                'node_id': node_id,
                                'ip': ip,
                                'port': port,
                                'model': node_info.get('model', 'unknown'),
                                'load': 0.0,
                                'tps': 0.0,
                                'uptime': 0,
                                'last_seen': int(time.time()),
                                'enriched': True,
                                'system_info': node_info.get('system', {}),
                                'dht_port': node_info.get('dht_port'),
                                'openai_compatible': node_info.get('openai_compatible', True)
                            }
                            
                            # Get current metrics if available
                            try:
                                async with session.get(f"http://{ip}:{port}/status") as status_response:
                                    if status_response.status == 200:
                                        status_data = await status_response.json()
                                        enriched_info.update({
                                            'load': status_data.get('load', 0.0),
                                            'tps': status_data.get('tps', 0.0),
                                            'uptime': status_data.get('uptime', 0),
                                            'total_tokens': status_data.get('total_tokens', 0)
                                        })
                            except Exception as e:
                                logger.debug(f"Could not get status for {node_id[:8]}...: {e}")
                            
                            # Broadcast enriched info
                            await self._broadcast_node_event("node_updated", enriched_info)
                            
                            logger.info(f"📊 Enriched and updated info for {node_id[:8]}... (model: {enriched_info['model']})")
                            
                        else:
                            logger.debug(f"Could not get info for {node_id[:8]}...: HTTP {response.status}")
                            
                except aiohttp.ClientError as e:
                    logger.debug(f"Connection error enriching {node_id[:8]}...: {e}")
                    
        except Exception as e:
            logger.debug(f"Error enriching joined node info for {node_id[:8]}...: {e}")
    
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

    async def _proactive_departure_detection(self):
        """Proactive detection of node departures with proper events"""
        try:
            # Get current DHT contacts
            current_contacts = set()
            if self.kademlia_node and self.kademlia_node.routing_table:
                contacts = self.kademlia_node.routing_table.get_all_contacts()
                current_contacts = {contact.node_id for contact in contacts}
            
            # Get published nodes
            published_nodes = set()
            try:
                all_nodes_data = await self.kademlia_node.find_value("all_nodes")
                if all_nodes_data:
                    if isinstance(all_nodes_data, list):
                        published_nodes = {node.get('node_id') for node in all_nodes_data 
                                         if isinstance(node, dict) and node.get('node_id')}
                    elif isinstance(all_nodes_data, dict) and all_nodes_data.get('node_id'):
                        published_nodes = {all_nodes_data['node_id']}
            except Exception as e:
                logger.debug(f"Could not check published nodes for departures: {e}")
            
            # Find nodes that have disappeared
            if hasattr(self, '_last_known_nodes'):
                # Nodes that were known but are no longer in DHT or published
                departed_nodes = self._last_known_nodes - current_contacts - published_nodes
                
                for departed_node_id in departed_nodes:
                    if departed_node_id != self.config.node_id:  # Don't report ourselves
                        # Verify the departure with health check
                        node_info = self._get_cached_node_info(departed_node_id)
                        
                        if node_info:
                            is_actually_down = await self._verify_node_actually_down(node_info)
                            
                            if is_actually_down:
                                departure_info = {
                                    'node_id': departed_node_id,
                                    'ip': node_info.get('ip'),
                                    'port': node_info.get('port'),
                                    'model': node_info.get('model'),
                                    'departure_reason': 'proactive_detection',
                                    'detected_by': self.config.node_id,
                                    'last_known_contact': self._last_contact_time.get(departed_node_id, 0),
                                    'reason': 'proactive_detection'
                                }
                            
                                # Broadcast via SSE (existing)
                                await self._broadcast_node_event("node_departed", departure_info)
                            
                                # Publish to DHT (NEW)
                                await self._publish_node_left_to_dht(departure_info)
                            
                                logger.info(f"👋 Proactively detected node departure: {departed_node_id[:8]}... (published to SSE + DHT)")
                            else:
                                # Node is still alive, update our records
                                logger.debug(f"Node {departed_node_id[:8]}... is still alive, updating records")
                                current_contacts.add(departed_node_id)
            
            # Update tracking
            self._last_known_nodes = current_contacts | published_nodes
            
            # Update last contact times
            current_time = time.time()
            for node_id in current_contacts:
                if not hasattr(self, '_last_contact_time'):
                    self._last_contact_time = {}
                self._last_contact_time[node_id] = current_time
            
        except Exception as e:
            logger.error(f"Error in proactive departure detection: {e}")

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

    async def _verify_node_actually_down(self, node_info: Dict[str, Any]) -> bool:
        """Verify a node is actually down with multiple checks"""
        try:
            import aiohttp
            
            ip = node_info.get('ip')
            port = node_info.get('port')
            
            if not ip or not port:
                return True  # Consider down if no contact info
            
            # Try multiple verification methods
            verification_methods = [
                self._http_health_check,
                self._dht_ping_check,
                self._tcp_connection_check
            ]
            
            for method in verification_methods:
                try:
                    is_alive = await method(ip, port)
                    if is_alive:
                        return False  # Node is alive
                except Exception as e:
                    logger.debug(f"Verification method {method.__name__} failed: {e}")
                    continue
            
            return True  # All verification methods failed
            
        except Exception as e:
            logger.debug(f"Error verifying node down status: {e}")
            return True

    async def _http_health_check(self, ip: str, port: int) -> bool:
        """HTTP health check"""
        import aiohttp
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(f"http://{ip}:{port}/health") as response:
                return response.status == 200

    async def _dht_ping_check(self, ip: str, port: int) -> bool:
        """DHT ping check"""
        if self.kademlia_node:
            # Try to ping the DHT port (usually HTTP port + 1)
            dht_port = port + 1
            contact = await self.kademlia_node._ping_node(ip, dht_port)
            return contact is not None
        return False

    async def _tcp_connection_check(self, ip: str, port: int) -> bool:
        """Basic TCP connection check"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), 
                timeout=5.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except:
            return False

    def _get_cached_node_info(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get cached node info for a node ID"""
        if not hasattr(self, '_node_info_cache'):
            self._node_info_cache = {}
        
        return self._node_info_cache.get(node_id)

    def _cache_node_info(self, node_id: str, node_info: Dict[str, Any]):
        """Cache node info for later use"""
        if not hasattr(self, '_node_info_cache'):
            self._node_info_cache = {}
        
        self._node_info_cache[node_id] = node_info

    async def _handle_node_departure(self, node_data: Dict[str, Any]):
        """Handle a detected node departure"""
        node_id = node_data['node_id']
        
        # Prepare departure info
        departure_info = {
            'node_id': node_id,
            'ip': node_data.get('ip'),
            'port': node_data.get('port'),
            'model': node_data.get('model'),
            'reason': 'health_check_failed',
            'detected_by': self.config.node_id,
            'last_seen': node_data.get('last_seen')
        }

        # Broadcast departure event via SSE (existing)
        await self._broadcast_node_event("node_left", departure_info)
        
        # Publish departure event to DHT (NEW)
        await self._publish_node_left_to_dht(departure_info)

    async def _periodic_new_node_detection(self):
        """Periodic check for new nodes that have joined the network (discovery only - NO EVENTS)"""
        try:
            # Get all published nodes from DHT
            all_nodes_data = await self.kademlia_node.find_value("all_nodes")
            if not all_nodes_data:
                return

            if not isinstance(all_nodes_data, list):
                all_nodes_data = [all_nodes_data]

            current_time = time.time()

            # Track known nodes to detect new ones (for internal tracking only)
            if not hasattr(self, '_known_node_ids'):
                self._known_node_ids = set()

            for node_data in all_nodes_data:
                if not isinstance(node_data, dict) or not node_data.get('node_id'):
                    continue
                    
                node_id = node_data['node_id']

                # Skip ourselves
                if node_id == self.config.node_id:
                    continue

                # Check if this is a new node we haven't seen before
                if node_id not in self._known_node_ids:
                    # Verify the node is fresh (published within last 2 minutes)
                    last_seen = node_data.get('last_seen', 0)
                    if current_time - last_seen < 120:
                        # Verify the node is actually reachable
                        is_reachable = await self._verify_node_reachable(node_data)
                        if is_reachable:
                            self._known_node_ids.add(node_id)
                            logger.debug(f"🔍 Discovered existing node: {node_id[:8]}... (last seen: {current_time - last_seen:.0f}s ago)")
                            # NO EVENT SENT - only internal tracking

            # Note: NO join events sent here - only the authoritative post-uvicorn join event is sent
                
        except Exception as e:
            logger.error(f"Error in periodic new node detection: {e}")

    async def _enhanced_node_discovery(self):
        """Enhanced node discovery with proper event handling (NO JOIN EVENTS)"""
        try:
            # Get all published nodes with retry logic
            all_nodes_data = await self._get_published_nodes_with_retry()
            
            if not all_nodes_data:
                logger.debug("No published nodes found during discovery")
                return
            
            current_time = time.time()
            discovered_nodes = []
            
            for node_data in all_nodes_data:
                if not isinstance(node_data, dict) or not node_data.get('node_id'):
                    continue
                
                node_id = node_data['node_id']
                
                # Skip ourselves
                if node_id == self.config.node_id:
                    continue
                
                # Check if node is fresh (within last 3 minutes)
                last_seen = node_data.get('last_seen', 0)
                if current_time - last_seen < 180:
                    # Verify node is actually reachable
                    is_reachable = await self._verify_node_reachable_enhanced(node_data)
                    
                    if is_reachable:
                        discovered_nodes.append(node_data)
                        
                        # Check if this is a new discovery (internal tracking only)
                        if not hasattr(self, '_known_node_ids'):
                            self._known_node_ids = set()
                        
                        if node_id not in self._known_node_ids:
                            self._known_node_ids.add(node_id)
                            logger.debug(f"🔍 Discovered existing node: {node_id[:8]}... via DHT scan")
                            # NO EVENT SENT - only internal tracking
            
            # Log discovery summary (no event needed)
            if discovered_nodes:
                logger.debug(f"Discovery scan completed: {len(discovered_nodes)} nodes found, {len(getattr(self, '_known_node_ids', set()))} total known")
            
        except Exception as e:
            logger.error(f"Error in enhanced node discovery: {e}")

    async def _get_published_nodes_with_retry(self, max_retries: int = 3) -> List[Dict[str, Any]]:
        """Get published nodes with retry logic"""
        for attempt in range(max_retries):
            try:
                all_nodes_data = await self.kademlia_node.find_value("all_nodes")
                if all_nodes_data:
                    if isinstance(all_nodes_data, list):
                        return all_nodes_data
                    else:
                        return [all_nodes_data]
                return []
            except Exception as e:
                logger.debug(f"Attempt {attempt + 1} failed to get published nodes: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
        
        return []

    async def _verify_node_reachable_enhanced(self, node_data: Dict[str, Any]) -> bool:
        """Enhanced node reachability verification"""
        try:
            import aiohttp
            
            ip = node_data.get('ip')
            port = node_data.get('port')
            
            if not ip or not port:
                return False
            
            # Try multiple endpoints with shorter timeout
            endpoints = ['/health', '/info', '/status']
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                for endpoint in endpoints:
                    try:
                        async with session.get(f"http://{ip}:{port}{endpoint}") as response:
                            if response.status in [200, 404, 405]:
                                return True
                    except:
                        continue
            
            return False
            
        except Exception as e:
            logger.debug(f"Enhanced reachability check failed for {node_data.get('node_id', 'unknown')[:8]}...: {e}")
            return False

    async def _verify_node_reachable(self, node_data: Dict[str, Any]) -> bool:
        """Verify that a detected node is actually reachable"""
        try:
            import aiohttp
            
            ip = node_data.get('ip')
            port = node_data.get('port')
            
            if not ip or not port:
                return False
            
            # Quick health check with short timeout
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://{ip}:{port}/health") as response:
                    if response.status == 200:
                        logger.debug(f"Node {node_data['node_id'][:8]}... is reachable")
                        return True
                        
        except Exception as e:
            logger.debug(f"Reachability check failed for {node_data['node_id'][:8]}...: {e}")
        
        return False

    async def force_update(self):
        """Force an immediate update of node info"""
        if self.running:
            await self._publish_node_info()
            self.last_forced_update = time.time()
            logger.info("Forced DHT update completed")
