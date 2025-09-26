import asyncio
import time
from typing import Dict, Any, List, Tuple
from common.utils import get_logger
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class EventBasedDHTPublisher:
    """Event-driven DHT publisher that responds to changes"""
    
    def __init__(self, config: InferenceConfig, metrics_callback):
        self.config = config
        self.metrics_callback = metrics_callback
        self.kademlia_node = None
        self.running = False
        
        # Event-driven state
        self.last_published_metrics = {}
        self.metrics_change_threshold = 0.05  # 5% change triggers update
        self.forced_update_interval = 60  # Force update every 60 seconds
        self.last_forced_update = 0
        
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
        self._validate_node_id_consistency()
        
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
        except Exception as e:
            logger.error(f"Failed to start DHT node: {e}")
            self.running = False
            raise
        
        # Start monitoring for changes
        self.monitor_task = asyncio.create_task(self._monitor_changes())
        
        # Publish initial state
        await self._publish_node_info()
        
        logger.info(f"Event-based DHT publisher started with hardware-based node ID: {self.config.node_id[:16]}...")
    
    async def stop(self):
        """Stop the event-based publisher"""
        self.running = False
        
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        # Remove our node from DHT
        await self._unpublish_node_info()
        
        self.kademlia_node = None
        logger.info("Event-based DHT publisher stopped")
    
    async def _monitor_changes(self):
        """Monitor for changes that should trigger updates"""
        while self.running:
            try:
                current_metrics = self.metrics_callback()
                current_time = time.time()
                
                # Check if metrics changed significantly
                should_update = self._should_update_metrics(current_metrics)
                
                # Force update periodically
                if current_time - self.last_forced_update > self.forced_update_interval:
                    should_update = True
                    self.last_forced_update = current_time
                    logger.info("Forcing periodic DHT update")
                
                if should_update:
                    await self._publish_node_info()
                    self.last_published_metrics = current_metrics.copy()
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring changes: {e}")
                await asyncio.sleep(10)
    
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
            }
            
            # Add hardware fingerprint summary for debugging
            if self.hardware_fingerprint:
                node_info['hardware_summary'] = self.hardware_fingerprint.get_fingerprint_summary()
            
            # Store under multiple keys
            keys = [
                f"model:{self.config.model_name}",
                f"node:{self.config.node_id}"
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
            
            logger.debug(f"Published hardware-based node info: load={metrics['load']:.3f}, tps={metrics['tps']:.2f}")
            
        except Exception as e:
            logger.error(f"Error publishing node info: {e}")
    
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
            'bootstrap_nodes': self.bootstrap_nodes
        }
        
        if self.hardware_fingerprint:
            info['hardware_fingerprint'] = self.hardware_fingerprint.get_fingerprint_summary()
        
        return info
    
    async def force_update(self):
        """Force an immediate update of node info"""
        if self.running:
            await self._publish_node_info()
            self.last_forced_update = time.time()
            logger.info("Forced DHT update completed")
