import asyncio
import hashlib
import json
import random
import time
import uuid
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from common.utils import get_logger
from common.port_utils import PortManager
from common.validation_utils import NodeValidator
from common.error_handler import ErrorHandler
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
        try:
            # Ensure both values are strings
            self_id_str = str(self.node_id) if self.node_id is not None else ""
            other_id_str = str(other_id) if other_id is not None else ""
            
            # Validate hex format
            if not self_id_str or not other_id_str:
                return float('inf')
                
            return int(self_id_str, 16) ^ int(other_id_str, 16)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid node ID format for distance calculation: {self.node_id} or {other_id}: {e}")
            return float('inf')  # Return max distance for invalid IDs

class KademliaNode:
    """Kademlia DHT Node implementation"""
    
    def __init__(self, node_id: str = None, port: int = 8001):
        # CRITICAL: Always use the provided node_id if it's valid (hardware-based)
        if node_id:
            if NodeValidator.validate_node_id(node_id):
                self.node_id = node_id
                logger.info(f"✅ Using provided hardware-based node ID: {node_id[:16]}...")
            else:
                logger.error(f"❌ Invalid node_id format provided: {node_id}")
                logger.warning("This should not happen with hardware-based node IDs")
                # Don't fallback - this indicates a serious issue
                raise ValueError(f"Invalid hardware-based node ID format: {node_id}")
        else:
            # Only generate random ID if no node_id provided (should not happen in normal operation)
            logger.warning("No node_id provided to KademliaNode, generating random ID")
            logger.warning("This indicates a configuration issue - hardware-based node ID should always be provided")
            self.node_id = self._generate_node_id()
        
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
        
        # Cleanup parameters - INCREASED INTERVALS
        self.cleanup_interval = 90  # Increased from 30 to 90 seconds
        self.cleanup_task = None
        self.last_cleanup = 0
        
        # Log final node ID for verification
        logger.debug(f"KademliaNode initialized with final node_id: {self.node_id[:16]}...")
        
    def _generate_node_id(self) -> str:
        """Generate a random 160-bit node ID"""
        return hashlib.sha1(str(random.random()).encode()).hexdigest()
    
    
    async def start(self, bootstrap_nodes: List[Tuple[str, int]] = None):
        """Start the Kademlia node"""
        self.running = True
        
        # Check if port is available before starting
        if not PortManager.is_udp_port_available(self.port):
            original_port = self.port
            self.port = PortManager.find_available_udp_port(self.port)
            logger.warning(f"DHT port {original_port} was in use, using {self.port} instead")
        
        # Start UDP server
        from dht.protocol import KademliaProtocol
        loop = asyncio.get_event_loop()
        
        # Store protocol reference
        def protocol_factory():
            self.protocol = KademliaProtocol(self)
            return self.protocol
            
        try:
            self.server = await loop.create_datagram_endpoint(
                protocol_factory,
                local_addr=('0.0.0.0', self.port)
            )
        except OSError as e:
            if e.errno == 48:  # Address already in use
                # Try to find an alternative port
                original_port = self.port
                self.port = PortManager.find_available_udp_port(self.port + 1)
                logger.warning(f"Port {original_port} still in use, retrying with port {self.port}")
                self.server = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=('0.0.0.0', self.port)
                )
            else:
                raise
        
        logger.info(f"Kademlia node {self.node_id[:8]} started on port {self.port}")
        
        # Start cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"🧹 Started cleanup task (interval: {self.cleanup_interval}s)")
        
        # Bootstrap if nodes provided
        if bootstrap_nodes:
            await self._bootstrap_basic(bootstrap_nodes)
    
    async def stop(self):
        """Stop the Kademlia node"""
        self.running = False
        
        # Stop cleanup task
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("🧹 Cleanup task stopped")
        
        if self.server:
            try:
                # self.server is a tuple (transport, protocol) from create_datagram_endpoint
                transport, protocol = self.server
                transport.close()
            except Exception as e:
                logger.warning(f"Error closing UDP transport: {e}")
            finally:
                self.server = None

    
    async def _cleanup_loop(self):
        """Periodically clean up stale contacts and verify connectivity"""
        while self.running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                if self.running:
                    await self._verify_contacts()
                    removed_count = self.routing_table.cleanup_stale_contacts()
                    await self._cleanup_storage()
                    self.last_cleanup = time.time()
                    
                    if removed_count > 0:
                        logger.info(f"🧹 Cleanup completed: removed {removed_count} stale contacts")
                    else:
                        logger.debug("🧹 Cleanup completed: no stale contacts found")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
    
    async def _verify_contacts(self):
        """Ping contacts to verify they're still alive"""
        all_contacts = self.routing_table.get_all_contacts()
        current_time = time.time()
        
        # Check contacts that haven't been seen recently (120+ seconds instead of 30)
        stale_contacts = [
            contact for contact in all_contacts
            if current_time - contact.last_seen > 120  # Increased from 30 to 120
        ]
        
        if not stale_contacts:
            logger.debug("🔍 No stale contacts to verify")
            return
        
        logger.info(f"🔍 Verifying {len(stale_contacts)} potentially stale contacts")
        
        # Reduce concurrency and add retry logic
        semaphore = asyncio.Semaphore(3)  # Reduced from 5 to 3
        tasks = [self._verify_contact_with_retry(contact, semaphore) for contact in stale_contacts]
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            alive_count = sum(1 for result in results if result is True)
            logger.info(f"🔍 Contact verification: {alive_count}/{len(stale_contacts)} contacts are alive")
    
    async def _verify_contact_with_retry(self, contact, semaphore):
        """Enhanced contact verification with routing table coordination"""
        async with semaphore:
            # Try DHT ping with retry
            for attempt in range(2):
                try:
                    verified_contact = await self._ping_node(contact.ip, contact.port)
                    if verified_contact:
                        self.routing_table.update_contact_seen(contact.node_id)
                        logger.debug(f"✅ DHT ping successful for {contact.node_id[:8]}... (attempt {attempt + 1})")
                        return True
                    
                    if attempt == 0:
                        await asyncio.sleep(3)  # Wait longer between attempts
                        
                except Exception as e:
                    logger.debug(f"DHT ping failed for {contact.node_id[:8]}... (attempt {attempt + 1}): {e}")
                    if attempt == 0:
                        await asyncio.sleep(3)
            
            # DHT ping failed - try HTTP health check as fallback
            try:
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                    # Try to find the HTTP port for this node
                    for http_port in [8000, 8002, 8004, 8006, 8008, 8010]:
                        try:
                            async with session.get(f"http://{contact.ip}:{http_port}/health") as response:
                                if response.status == 200:
                                    logger.info(f"✅ HTTP health check successful for {contact.node_id[:8]}... on port {http_port}")
                                    # Update last_seen since node is actually alive
                                    self.routing_table.update_contact_seen(contact.node_id)
                                    return True
                        except:
                            continue
            except Exception as e:
                logger.debug(f"HTTP health check failed for {contact.node_id[:8]}...: {e}")
            
            # Node is confirmed unreachable - trigger leave event
            logger.info(f"💀 Contact {contact.node_id[:8]}... confirmed unreachable, triggering leave event")
            await self.handle_network_leave_event(contact.node_id, 'verification_failed')
            
            return False
    
    async def _cleanup_storage(self):
        """Clean up expired storage entries"""
        current_time = time.time()
        expired_keys = []
        
        for key, stored_item in self.storage.items():
            if current_time - stored_item['timestamp'] > self.ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.storage[key]
            logger.debug(f"🗑️ Removed expired storage key: {key}")
        
        if expired_keys:
            logger.info(f"🗑️ Cleaned up {len(expired_keys)} expired storage entries")
    
    def get_cleanup_stats(self) -> Dict[str, Any]:
        """Get cleanup statistics"""
        return {
            "last_cleanup": self.last_cleanup,
            "cleanup_interval": self.cleanup_interval,
            "routing_table_stats": self.routing_table.get_stats(),
            "storage_entries": len(self.storage)
        }
    
    async def _bootstrap_basic(self, bootstrap_nodes: List[Tuple[str, int]]):
        """Basic bootstrap without event emission"""
        successful_connections = []
        
        for ip, port in bootstrap_nodes:
            try:
                logger.info(f"🔗 Attempting to connect to bootstrap node {ip}:{port}")
                
                # Ping bootstrap node to get its ID
                contact = await self._ping_node(ip, port)
                if contact and contact.node_id:
                    # Validate the node ID before adding
                    if NodeValidator.validate_node_id(str(contact.node_id)):
                        self.routing_table.add_contact(contact)
                        successful_connections.append((ip, port, contact.node_id))
                        
                        # Find nodes close to ourselves
                        await self.find_node(self.node_id)
                    else:
                        logger.warning(f"Bootstrap node {ip}:{port} returned invalid node ID: {contact.node_id}")
                else:
                    logger.warning(f"Failed to get valid contact from bootstrap node {ip}:{port}")
            except Exception as e:
                logger.warning(f"Failed to bootstrap from {ip}:{port}: {e}")
        
        if successful_connections:
            logger.info(f"✅ Successfully connected to {len(successful_connections)} bootstrap nodes")
        else:
            logger.warning("❌ Failed to connect to any bootstrap nodes")
    
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
        
        try:
            response = await self.protocol.send_request(message, (ip, port))
            if response and response.get('pong'):
                # Try to get sender_id from response root or data
                sender_id = response.get('sender_id') or response.get('data', {}).get('sender_id')
                if sender_id and NodeValidator.validate_node_id(str(sender_id)):
                    return Contact(str(sender_id), ip, port)
                else:
                    logger.warning(f"Invalid sender_id received from {ip}:{port}: {sender_id}")
            return None
        except Exception as e:
            logger.error(f"Error pinging node {ip}:{port}: {e}")
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
    
    async def handle_network_join_event(self, node_id: str, ip: str, port: int, join_source: str = 'network') -> bool:
        """Handle network-level join events and update routing table"""
        if node_id == self.node_id:
            return False  # Don't handle our own joins
        
        # Validate the node ID format
        if not NodeValidator.validate_node_id(node_id):
            logger.warning(f"Invalid node ID in join event: {node_id}")
            return False
        
        # Create contact and update routing table
        contact = Contact(node_id, ip, port)
        is_new_contact = self.routing_table.handle_node_join(contact, join_source)
        
        return is_new_contact

    async def handle_network_leave_event(self, node_id: str, leave_reason: str = 'network') -> bool:
        """Handle network-level leave events and update routing table"""
        if node_id == self.node_id:
            return False  # Don't handle our own leaves
        
        # Remove from routing table
        contact_removed = self.routing_table.handle_node_leave(node_id, leave_reason)
        
        return contact_removed

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

