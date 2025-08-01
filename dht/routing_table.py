import time
from typing import List, Dict, Optional, TYPE_CHECKING
from common.utils import get_logger

if TYPE_CHECKING:
    from dht.kademlia_node import Contact

logger = get_logger(__name__)

class KBucket:
    """K-bucket for storing contacts"""
    
    def __init__(self, k: int = 20):
        self.k = k
        self.contacts: List['Contact'] = []
        self.last_updated = time.time()
    
    def add_contact(self, contact: 'Contact') -> bool:
        """Add a contact to the bucket"""
        # Remove if already exists
        self.contacts = [c for c in self.contacts if c.node_id != contact.node_id]
        
        # Add to front
        self.contacts.insert(0, contact)
        
        # Trim to k size
        if len(self.contacts) > self.k:
            self.contacts = self.contacts[:self.k]
        
        self.last_updated = time.time()
        return True
    
    def remove_contact(self, node_id: str):
        """Remove a contact from the bucket"""
        self.contacts = [c for c in self.contacts if c.node_id != node_id]
    
    def get_contacts(self) -> List['Contact']:
        """Get all contacts in the bucket"""
        return self.contacts.copy()

class RoutingTable:
    """Kademlia routing table with k-buckets"""
    
    def __init__(self, node_id: str, k: int = 20):
        self.node_id = node_id
        self.k = k
        self.buckets: Dict[int, KBucket] = {}
    
    def add_contact(self, contact: 'Contact'):
        """Add a contact to the appropriate bucket"""
        if contact.node_id == self.node_id:
            return  # Don't add ourselves
        
        bucket_index = self._get_bucket_index(contact.node_id)
        
        if bucket_index not in self.buckets:
            self.buckets[bucket_index] = KBucket(self.k)
        
        self.buckets[bucket_index].add_contact(contact)
        logger.debug(f"Added contact {contact.node_id[:8]} to bucket {bucket_index}")
    
    def remove_contact(self, node_id: str):
        """Remove a contact from the routing table"""
        bucket_index = self._get_bucket_index(node_id)
        if bucket_index in self.buckets:
            self.buckets[bucket_index].remove_contact(node_id)
    
    def find_closest_contacts(self, target_id: str, count: int) -> List['Contact']:
        """Find the closest contacts to a target ID"""
        all_contacts = []
        
        # Collect all contacts
        for bucket in self.buckets.values():
            all_contacts.extend(bucket.get_contacts())
        
        # Sort by distance to target
        all_contacts.sort(key=lambda c: c.distance(target_id))
        
        return all_contacts[:count]
    
    def _get_bucket_index(self, node_id: str) -> int:
        """Get the bucket index for a node ID"""
        try:
            distance = int(self.node_id, 16) ^ int(node_id, 16)
            if distance == 0:
                return 0
            return distance.bit_length() - 1
        except ValueError:
            logger.error(f"Invalid node ID format: {node_id}")
            return 0  # Default bucket
    
    def get_all_contacts(self) -> List['Contact']:
        """Get all contacts in the routing table"""
        all_contacts = []
        for bucket in self.buckets.values():
            all_contacts.extend(bucket.get_contacts())
        return all_contacts
