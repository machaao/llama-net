import asyncio
import hashlib
import json
import random
import time
import uuid
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from common.utils import get_logger
from dht.routing_table import RoutingTable

logger = get_logger(__name__)

@dataclass
class Contact:
    """Represents a contact in the Kademlia network"""
    node_id: str
    ip: str
    port: int
    last_seen: float = 0
    
    def __post_init__(self):
        if self.last_seen == 0:
            self.last_seen = time.time()
    
    def distance(self, other_id: str) -> int:
        """Calculate XOR distance between this contact and another node ID"""
        return int(self.node_id, 16) ^ int(other_id, 16)

class KademliaNode:
    """Kademlia DHT Node implementation"""
    
    def __init__(self, node_id: str = None, port: int = 8001):
        self.node_id = node_id or self._generate_node_id()
        self.port = port
        self.routing_table = RoutingTable(self.node_id)
        self.storage: Dict[str, Any] = {}
        self.server = None
        self.protocol = None
        self.running = False
        
        # Kademlia parameters
        self.k = 20  # bucket size
        self.alpha = 3  # concurrency parameter
        self.ttl = 86400  # 24 hours in seconds
        
    def _generate_node_id(self) -> str:
        """Generate a random 160-bit node ID"""
        return hashlib.sha1(str(random.random()).encode()).hexdigest()
    
    async def start(self, bootstrap_nodes: List[Tuple[str, int]] = None):
        """Start the Kademlia node"""
        self.running = True
        
        # Start UDP server
        from dht.protocol import KademliaProtocol
        loop = asyncio.get_event_loop()
        
        # Store protocol reference
        def protocol_factory():
            self.protocol = KademliaProtocol(self)
            return self.protocol
            
        self.server = await loop.create_datagram_endpoint(
            protocol_factory,
            local_addr=('0.0.0.0', self.port)
        )
        
        logger.info(f"Kademlia node {self.node_id[:8]} started on port {self.port}")
        
        # Bootstrap if nodes provided
        if bootstrap_nodes:
            await self._bootstrap(bootstrap_nodes)
    
    async def stop(self):
        """Stop the Kademlia node"""
        self.running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
    
    async def _bootstrap(self, bootstrap_nodes: List[Tuple[str, int]]):
        """Bootstrap by connecting to existing nodes"""
        for ip, port in bootstrap_nodes:
            try:
                # Ping bootstrap node to get its ID
                contact = await self._ping_node(ip, port)
                if contact:
                    self.routing_table.add_contact(contact)
                    # Find nodes close to ourselves
                    await self.find_node(self.node_id)
            except Exception as e:
                logger.warning(f"Failed to bootstrap from {ip}:{port}: {e}")
    
    async def store(self, key: str, value: Any) -> bool:
        """Store a key-value pair in the DHT"""
        # Find closest nodes to the key
        closest_nodes = await self.find_node(key)
        
        # Store on closest nodes
        success_count = 0
        for contact in closest_nodes[:self.k]:
            try:
                success = await self._store_on_node(contact, key, value)
                if success:
                    success_count += 1
            except Exception as e:
                logger.warning(f"Failed to store on {contact.node_id[:8]}: {e}")
        
        # Also store locally if we're among the closest
        key_hash = hashlib.sha1(key.encode()).hexdigest()
        if self._am_closest_to_key(key_hash):
            self.storage[key] = {
                'value': value,
                'timestamp': time.time()
            }
            success_count += 1
        
        return success_count > 0
    
    async def find_value(self, key: str) -> Optional[Any]:
        """Find a value in the DHT"""
        # Check local storage first
        if key in self.storage:
            stored_item = self.storage[key]
            if time.time() - stored_item['timestamp'] < self.ttl:
                return stored_item['value']
            else:
                del self.storage[key]  # Expired
        
        # Find closest nodes to the key
        closest_nodes = await self.find_node(key)
        
        # Query nodes for the value
        for contact in closest_nodes:
            try:
                value = await self._find_value_on_node(contact, key)
                if value is not None:
                    return value
            except Exception as e:
                logger.warning(f"Failed to query {contact.node_id[:8]}: {e}")
        
        return None
    
    async def find_node(self, target_id: str) -> List[Contact]:
        """Find nodes closest to target ID"""
        target_hash = hashlib.sha1(target_id.encode()).hexdigest()
        
        # Start with closest known nodes
        closest = self.routing_table.find_closest_contacts(target_hash, self.k)
        queried = set()
        
        while True:
            # Select alpha nodes to query
            to_query = [c for c in closest if c.node_id not in queried][:self.alpha]
            if not to_query:
                break
            
            # Query nodes concurrently
            tasks = [self._find_node_on_contact(contact, target_hash) for contact in to_query]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            new_contacts = []
            for i, result in enumerate(results):
                queried.add(to_query[i].node_id)
                if isinstance(result, list):
                    new_contacts.extend(result)
            
            # Add new contacts to routing table and closest list
            for contact in new_contacts:
                self.routing_table.add_contact(contact)
                if contact not in closest:
                    closest.append(contact)
            
            # Sort by distance and keep closest k
            closest.sort(key=lambda c: c.distance(target_hash))
            closest = closest[:self.k]
        
        return closest
    
    def _am_closest_to_key(self, key_hash: str) -> bool:
        """Check if we are among the closest nodes to a key"""
        closest = self.routing_table.find_closest_contacts(key_hash, self.k)
        my_distance = int(self.node_id, 16) ^ int(key_hash, 16)
        
        if len(closest) < self.k:
            return True
        
        return any(my_distance <= contact.distance(key_hash) for contact in closest)
    
    async def _ping_node(self, ip: str, port: int) -> Optional[Contact]:
        """Ping a node and return contact info"""
        message = {
            'type': 'ping',
            'id': str(uuid.uuid4()),
            'sender_id': self.node_id
        }
        
        response = await self.protocol.send_request(message, (ip, port))
        if response and response.get('pong'):
            return Contact(response.get('sender_id'), ip, port)
        return None
    
    async def _store_on_node(self, contact: Contact, key: str, value: Any) -> bool:
        """Store key-value on a specific node"""
        message = {
            'type': 'store',
            'id': str(uuid.uuid4()),
            'sender_id': self.node_id,
            'key': key,
            'value': value
        }
        
        response = await self.protocol.send_request(message, (contact.ip, contact.port))
        return response and response.get('stored', False)
    
    async def _find_value_on_node(self, contact: Contact, key: str) -> Optional[Any]:
        """Find value on a specific node"""
        message = {
            'type': 'find_value',
            'id': str(uuid.uuid4()),
            'sender_id': self.node_id,
            'key': key
        }
        
        response = await self.protocol.send_request(message, (contact.ip, contact.port))
        if response and 'value' in response:
            return response['value']
        return None
    
    async def _find_node_on_contact(self, contact: Contact, target_id: str) -> List[Contact]:
        """Find nodes on a specific contact"""
        message = {
            'type': 'find_node',
            'id': str(uuid.uuid4()),
            'sender_id': self.node_id,
            'target_id': target_id
        }
        
        response = await self.protocol.send_request(message, (contact.ip, contact.port))
        if response and 'contacts' in response:
            contacts = []
            for contact_data in response['contacts']:
                contacts.append(Contact(
                    contact_data['node_id'],
                    contact_data['ip'],
                    contact_data['port']
                ))
            return contacts
        return []

