import asyncio
import time
from typing import List, Optional, Dict, Any, Tuple
from dht.kademlia_node import KademliaNode
from common.models import NodeInfo
from common.utils import get_logger
from client.discovery import DiscoveryInterface

logger = get_logger(__name__)

class DHTDiscovery(DiscoveryInterface):
    """Client for discovering nodes via Kademlia DHT"""
    
    def __init__(self, bootstrap_nodes: str = "", dht_port: int = 8001):
        self.bootstrap_nodes = self._parse_bootstrap_nodes(bootstrap_nodes)
        self.dht_port = dht_port
        self.kademlia_node = None
        self.cache_time = 0
        self.cache_ttl = 5  # Cache results for 5 seconds
        self.nodes_cache: List[NodeInfo] = []
        self._cache_is_model_specific = False
        self.known_node_ids = set()  # Track known nodes
        
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
        """Start the DHT client"""
        if self.kademlia_node:
            return
        
        self.kademlia_node = KademliaNode(port=self.dht_port)
        await self.kademlia_node.start(self.bootstrap_nodes)
        logger.info("DHT discovery client started")
    
    async def stop(self):
        """Stop the DHT client"""
        if self.kademlia_node:
            await self.kademlia_node.stop()
            self.kademlia_node = None
    
    async def get_nodes(self, model: Optional[str] = None, force_refresh: bool = False) -> List[NodeInfo]:
        """Get all active nodes, optionally filtered by model"""
        if not self.kademlia_node:
            await self.start()
        
        current_time = time.time()
        
        # Check if we need to refresh the cache
        if force_refresh or current_time - self.cache_time > self.cache_ttl:
            await self._refresh_nodes(model)
        
        # Filter by model if needed and not already filtered
        if model and not self._cache_is_model_specific:
            return [node for node in self.nodes_cache if node.model == model]
        
        return self.nodes_cache
    
    async def _get_routing_table_contacts(self) -> List[NodeInfo]:
        """Get contacts from DHT routing table and convert to NodeInfo"""
        if not self.kademlia_node:
            return []
        
        contacts = self.kademlia_node.routing_table.get_all_contacts()
        node_infos = []
        
        for contact in contacts:
            try:
                # Create basic NodeInfo from contact
                node_info = NodeInfo(
                    node_id=contact.node_id,
                    ip=contact.ip,
                    port=8000,  # Default HTTP port assumption
                    model="unknown",
                    last_seen=int(contact.last_seen)
                )
                
                # Try to get actual node info via HTTP
                http_port = await self._probe_http_port(contact.ip, contact.node_id)
                if http_port:
                    node_info.port = http_port
                    
                    # Get model info
                    model_info = await self._get_node_model(contact.ip, http_port)
                    if model_info:
                        node_info.model = model_info
                
                node_infos.append(node_info)
                
            except Exception as e:
                logger.debug(f"Failed to get info for contact {contact.node_id[:8]}: {e}")
        
        return node_infos

    async def _probe_http_port(self, ip: str, node_id: str) -> Optional[int]:
        """Probe for the correct HTTP port for a node"""
        import aiohttp
        
        # Try common HTTP ports
        test_ports = [8000, 8002, 8004, 8006, 8008, 8010]
        
        for port in test_ports:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                    async with session.get(f"http://{ip}:{port}/info") as resp:
                        if resp.status == 200:
                            info = await resp.json()
                            # Verify this is the correct node by checking node_id
                            if info.get('node_id') == node_id:
                                logger.debug(f"Found HTTP port {port} for node {node_id[:8]}...")
                                return port
            except:
                continue
        
        logger.warning(f"Could not find HTTP port for node {node_id[:8]}... on {ip}")
        return None

    async def _get_node_model(self, ip: str, port: int) -> Optional[str]:
        """Get model information from a node"""
        import aiohttp
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                async with session.get(f"http://{ip}:{port}/info") as resp:
                    if resp.status == 200:
                        info = await resp.json()
                        return info.get('model', 'unknown')
        except:
            pass
        
        return 'unknown'

    async def _get_published_nodes(self, model: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get published nodes from DHT storage"""
        nodes = []
        
        if model:
            key = f"model:{model}"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                if isinstance(node_data, list):
                    nodes.extend(node_data)
                else:
                    nodes.append(node_data)
            self._cache_is_model_specific = True
        else:
            key = "all_nodes"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                if isinstance(node_data, list):
                    nodes.extend(node_data)
                else:
                    nodes.append(node_data)
            self._cache_is_model_specific = False
        
        return nodes

    async def _refresh_nodes(self, model: Optional[str] = None):
        """Refresh the nodes cache from DHT with enhanced discovery and fallback"""
        try:
            # Use a dict to ensure uniqueness by node_id (not ip:port)
            unique_nodes = {}
            
            # 1. First try to get published nodes from DHT storage
            published_nodes = await self._get_published_nodes(model)
            
            # Also try to get from model-specific keys if no model filter
            if not model:
                try:
                    # Try common model names
                    for common_model in ["llamanet", "llama", "mistral"]:
                        model_nodes = await self._get_published_nodes(common_model)
                        published_nodes.extend(model_nodes)
                except Exception as e:
                    logger.debug(f"Error getting model-specific nodes: {e}")
            
            # Process published nodes
            for node_data in published_nodes:
                if isinstance(node_data, dict) and node_data.get('node_id'):
                    node_id = node_data['node_id']
                    # Use longer timeout for published data (2 minutes)
                    if time.time() - node_data.get('last_seen', 0) < 120:
                        try:
                            node_info = NodeInfo(**node_data)
                            unique_nodes[node_id] = {
                                'node': node_info,
                                'source': 'published',
                                'priority': 1
                            }
                            logger.debug(f"Added published node: {node_id[:8]}... at {node_info.ip}:{node_info.port}")
                        except Exception as e:
                            logger.warning(f"Failed to parse published node: {e}")
            
            # 2. If no published nodes found, fall back to DHT routing table contacts
            if not unique_nodes:
                logger.warning("No published nodes found, falling back to DHT routing table contacts")
                routing_contacts = await self._get_routing_table_contacts()
                
                for node_info in routing_contacts:
                    if node_info.node_id not in unique_nodes:
                        unique_nodes[node_info.node_id] = {
                            'node': node_info,
                            'source': 'dht_contact',
                            'priority': 2
                        }
                        logger.debug(f"Added DHT contact: {node_info.node_id[:8]}... at {node_info.ip}:{node_info.port}")
            
            # Convert to list and update cache
            self.nodes_cache = [entry['node'] for entry in unique_nodes.values()]
            
            # Track new nodes
            for node_id, entry in unique_nodes.items():
                if node_id not in self.known_node_ids:
                    node = entry['node']
                    source = entry['source']
                    logger.info(f"🆕 New {source} node: {node_id[:12]}... ({node.ip}:{node.port}) - Model: {node.model}")
                    self.known_node_ids.add(node_id)
            
            self.cache_time = time.time()
            
            # Enhanced logging with source breakdown
            published_count = len([e for e in unique_nodes.values() if e['source'] == 'published'])
            dht_contact_count = len([e for e in unique_nodes.values() if e['source'] == 'dht_contact'])
            
            logger.info(f"Refreshed nodes cache: {len(self.nodes_cache)} total nodes ({published_count} published, {dht_contact_count} DHT contacts)")
            
            # Log all discovered nodes for debugging
            for node in self.nodes_cache:
                source = next((e['source'] for e in unique_nodes.values() if e['node'].node_id == node.node_id), 'unknown')
                logger.debug(f"Available {source} node: {node.node_id[:8]}... at {node.ip}:{node.port} (model: {node.model})")
            
            if len(published_nodes) > 0 and published_count == 0:
                logger.warning("Published nodes found but none were valid - possible data corruption")
            
            # Log DHT storage keys for debugging
            if self.kademlia_node and hasattr(self.kademlia_node, 'storage'):
                storage_keys = list(self.kademlia_node.storage.keys())
                logger.debug(f"DHT storage keys available: {storage_keys}")
            
        except Exception as e:
            logger.error(f"Error refreshing nodes from DHT: {e}")
    
    async def find_specific_node(self, node_id: str) -> Optional[NodeInfo]:
        """Find a specific node by ID"""
        if not self.kademlia_node:
            await self.start()
        
        try:
            key = f"node:{node_id}"
            node_data = await self.kademlia_node.find_value(key)
            if node_data:
                return NodeInfo(**node_data)
        except Exception as e:
            logger.error(f"Error finding node {node_id}: {e}")
        
        return None
