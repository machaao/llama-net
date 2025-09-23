import asyncio
import time
import json
from typing import Dict, Any, List, Tuple
from dht.kademlia_node import KademliaNode
from common.utils import get_logger, get_host_ip
from inference_node.config import InferenceConfig

logger = get_logger(__name__)

class DHTPublisher:
    """Publishes inference node info to Kademlia DHT"""
    
    def __init__(self, config: InferenceConfig, metrics_callback):
        self.config = config
        self.metrics_callback = metrics_callback
        self.kademlia_node = None
        self.running = False
        self.publish_task = None
        
        # Parse bootstrap nodes from config
        self.bootstrap_nodes = self._parse_bootstrap_nodes(config.bootstrap_nodes)
        
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
    
    async def start(self):
        """Start the DHT publisher using shared DHT service"""
        if self.running:
            return
        
        self.running = True
        
        # Use shared DHT service instead of creating new instance
        from common.dht_service import SharedDHTService
        dht_service = SharedDHTService()
        
        try:
            self.kademlia_node = await dht_service.initialize(
                node_id=self.config.node_id,
                port=self.config.dht_port,
                bootstrap_nodes=self.bootstrap_nodes
            )
            
            # Update config with actual port used (in case it changed)
            if self.kademlia_node.port != self.config.dht_port:
                logger.info(f"DHT port changed from {self.config.dht_port} to {self.kademlia_node.port}")
                self.config.dht_port = self.kademlia_node.port
                
        except Exception as e:
            logger.error(f"Failed to start DHT node: {e}")
            self.running = False
            raise
        
        # Start publishing loop
        self.publish_task = asyncio.create_task(self._publish_loop())
        
        logger.info(f"DHT publisher started using shared service")
        
        if self.bootstrap_nodes:
            logger.info(f"🌐 Joining DHT network via bootstrap nodes: {self.bootstrap_nodes}")
        else:
            logger.info(f"🌟 Starting as DHT bootstrap node")
    
    async def stop(self):
        """Stop the DHT publisher"""
        self.running = False
        
        if self.publish_task:
            self.publish_task.cancel()
            try:
                await self.publish_task
            except asyncio.CancelledError:
                pass
        
        # Don't stop the shared DHT service here - let the main service handle it
        self.kademlia_node = None
    
    async def _publish_loop(self):
        """Periodically publish node info to DHT"""
        while self.running:
            try:
                await self._publish_node_info()
                await asyncio.sleep(self.config.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in publish loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying
    
    async def _publish_node_info(self):
        """Publish current node info to DHT with proper aggregation"""
        # Get current metrics
        metrics = self.metrics_callback()
        
        # Create node info
        node_info = {
            'node_id': self.config.node_id,
            'ip': get_host_ip(),
            'port': self.config.port,  # HTTP port for inference API
            'model': self.config.model_name,
            'load': metrics['load'],
            'tps': metrics['tps'],
            'uptime': metrics['uptime'],
            'last_seen': int(time.time()),
            'dht_port': self.config.dht_port
        }
        
        # Store individual node data
        individual_keys = [
            f"model:{self.config.model_name}",
            f"node:{self.config.node_id}"
        ]
        
        for key in individual_keys:
            try:
                success = await self.kademlia_node.store(key, node_info)
                if success:
                    logger.debug(f"Published node info under key: {key}")
                else:
                    logger.warning(f"Failed to publish under key: {key}")
            except Exception as e:
                logger.error(f"Error publishing to key {key}: {e}")
        
        # For all_nodes, we need to aggregate with existing data
        await self._update_all_nodes_registry(node_info)

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
