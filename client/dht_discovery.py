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
            # Try to get detailed info from the contact
            try:
                # Create basic NodeInfo from contact
                node_info = NodeInfo(
                    node_id=contact.node_id,
                    ip=contact.ip,
                    port=8000,  # Default HTTP port assumption
                    model="unknown",  # Will be updated when we get actual node info
                    last_seen=int(contact.last_seen)
                )
                
                # Try to get actual node info via HTTP
                try:
                    import aiohttp
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                        # Try common HTTP ports
                        for http_port in [8000, 8002, 8004]:
                            try:
                                async with session.get(f"http://{contact.ip}:{http_port}/info") as resp:
                                    if resp.status == 200:
                                        info = await resp.json()
                                        node_info.port = http_port
                                        node_info.model = info.get('model', 'unknown')
                                        break
                            except:
                                continue
                except:
                    pass
                
                node_infos.append(node_info)
                
            except Exception as e:
                logger.debug(f"Failed to get info for contact {contact.node_id[:8]}: {e}")
        
        return node_infos

    async def _refresh_nodes(self, model: Optional[str] = None):
        """Refresh the nodes cache from DHT"""
        try:
            nodes = []
            
            # Get nodes from DHT storage (published data)
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
            
            # ALSO get contacts from routing table
            routing_contacts = await self._get_routing_table_contacts()
            
            # Convert published data to NodeInfo objects
            seen_nodes = set()
            self.nodes_cache = []
            
            # Process published node data first (higher priority)
            for node_data in nodes:
                if isinstance(node_data, dict):
                    node_id = node_data.get('node_id')
                    if node_id and node_id not in seen_nodes:
                        try:
                            node_info = NodeInfo(**node_data)
                            if time.time() - node_info.last_seen < 60:
                                self.nodes_cache.append(node_info)
                                seen_nodes.add(node_id)
                                
                                if node_id not in self.known_node_ids:
                                    logger.info(f"🆕 New published node: {node_id[:12]}... ({node_data.get('ip')}:{node_data.get('port')}) - Model: {node_data.get('model')}")
                                    self.known_node_ids.add(node_id)
                        except Exception as e:
                            logger.warning(f"Failed to parse published node data: {e}")
            
            # Add routing table contacts that aren't already known
            for contact_node in routing_contacts:
                if contact_node.node_id not in seen_nodes:
                    # Filter by model if specified
                    if not model or contact_node.model == model or contact_node.model == "unknown":
                        self.nodes_cache.append(contact_node)
                        seen_nodes.add(contact_node.node_id)
                        
                        if contact_node.node_id not in self.known_node_ids:
                            logger.info(f"🔗 New DHT contact node: {contact_node.node_id[:12]}... ({contact_node.ip}:{contact_node.port}) - Model: {contact_node.model}")
                            self.known_node_ids.add(contact_node.node_id)
            
            self.cache_time = time.time()
            logger.info(f"Refreshed nodes cache: {len(self.nodes_cache)} total nodes ({len(nodes)} published + {len(routing_contacts)} DHT contacts)")
            
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
