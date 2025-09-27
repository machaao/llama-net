import time
from typing import List, Dict, Optional, TYPE_CHECKING, Any
from common.utils import get_logger
from common.validation_utils import NodeValidator

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
        self.contact_timeout = 180  # Increased from 60 to 180 seconds (3 minutes)
    
    def add_contact(self, contact: 'Contact'):
        """Add a contact to the appropriate bucket"""
        if contact.node_id == self.node_id:
            return  # Don't add ourselves
        
        bucket_index = self._get_bucket_index(contact.node_id)
        
        if bucket_index not in self.buckets:
            self.buckets[bucket_index] = KBucket(self.k)
        
        # Check if this is a new contact before adding
        existing_contact_ids = [c.node_id for c in self.buckets[bucket_index].contacts]
        is_new_contact = contact.node_id not in existing_contact_ids
        
        # IMPORTANT: Remove old contact with same node_id (not ip:port combo)
        # This ensures we update the contact info if ports change
        self.buckets[bucket_index].contacts = [
            c for c in self.buckets[bucket_index].contacts 
            if c.node_id != contact.node_id
        ]
        
        self.buckets[bucket_index].add_contact(contact)
        
        if is_new_contact:
            logger.info(f"🔗 New DHT contact added: {contact.node_id[:12]}... ({contact.ip}:{contact.port})")
        else:
            logger.debug(f"Updated contact {contact.node_id[:8]} in bucket {bucket_index} - new address: {contact.ip}:{contact.port}")
    
    def remove_contact(self, node_id: str):
        """Remove a contact from the routing table"""
        bucket_index = self._get_bucket_index(node_id)
        if bucket_index in self.buckets:
            self.buckets[bucket_index].remove_contact(node_id)
    
    def cleanup_stale_contacts(self):
        """Remove contacts that haven't been seen recently"""
        current_time = time.time()
        removed_count = 0
        
        for bucket_index, bucket in list(self.buckets.items()):
            original_count = len(bucket.contacts)
            
            # Filter out stale contacts
            stale_contacts = []
            active_contacts = []
            
            for contact in bucket.contacts:
                if current_time - contact.last_seen < self.contact_timeout:
                    active_contacts.append(contact)
                else:
                    stale_contacts.append(contact)
            
            bucket.contacts = active_contacts
            
            # Log removed contacts
            for contact in stale_contacts:
                logger.info(f"🧹 Removed stale contact: {contact.node_id[:12]}... ({contact.ip}:{contact.port}) - last seen {int(current_time - contact.last_seen)}s ago")
            
            removed = original_count - len(bucket.contacts)
            if removed > 0:
                removed_count += removed
            
            # Remove empty buckets
            if not bucket.contacts:
                del self.buckets[bucket_index]
        
        if removed_count > 0:
            logger.info(f"🧹 Total cleanup: removed {removed_count} stale contacts")
        
        return removed_count
    
    def update_contact_seen(self, node_id: str):
        """Update last_seen time for a contact"""
        for bucket in self.buckets.values():
            for contact in bucket.contacts:
                if contact.node_id == node_id:
                    contact.last_seen = time.time()
                    logger.debug(f"📡 Updated last_seen for contact {node_id[:8]}...")
                    return True
        return False
    
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
            # Ensure both are strings
            self_id_str = str(self.node_id)
            node_id_str = str(node_id)
            
            distance = int(self_id_str, 16) ^ int(node_id_str, 16)
            if distance == 0:
                return 0
            return distance.bit_length() - 1
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid node ID format for bucket calculation: {self.node_id} or {node_id}: {e}")
            return 0  # Default bucket
    
    def get_all_contacts(self) -> List['Contact']:
        """Get all contacts in the routing table"""
        all_contacts = []
        for bucket in self.buckets.values():
            all_contacts.extend(bucket.get_contacts())
        return all_contacts
    
    def get_unique_contacts(self) -> List['Contact']:
        """Get all unique contacts (deduplicated by node_id)"""
        seen_ids = set()
        unique_contacts = []
        
        for bucket in self.buckets.values():
            for contact in bucket.get_contacts():
                if contact.node_id not in seen_ids:
                    seen_ids.add(contact.node_id)
                    unique_contacts.append(contact)
        
        return unique_contacts
    
    def handle_node_join(self, contact: 'Contact', join_source: str = 'unknown') -> bool:
        """Handle explicit node join event with enhanced tracking"""
        if contact.node_id == self.node_id:
            return False  # Don't add ourselves
        
        # Validate contact before adding
        if not NodeValidator.validate_contact(contact):
            logger.warning(f"Invalid contact in join event: {contact.node_id}")
            return False
        
        # Check if this is truly a new contact
        existing_contact = self.get_contact_by_id(contact.node_id)
        is_new_contact = existing_contact is None
        
        # Add/update the contact
        self.add_contact(contact)
        
        if is_new_contact:
            logger.info(f"🆕 Node joined DHT: {contact.node_id[:12]}... ({contact.ip}:{contact.port}) via {join_source}")
        else:
            logger.debug(f"🔄 Updated existing contact: {contact.node_id[:8]}... new address: {contact.ip}:{contact.port}")
        
        return is_new_contact

    def handle_node_leave(self, node_id: str, leave_reason: str = 'unknown') -> bool:
        """Handle explicit node leave event"""
        if node_id == self.node_id:
            return False  # Don't remove ourselves
        
        # Validate node ID
        if not NodeValidator.validate_node_id(node_id):
            logger.warning(f"Invalid node ID in leave event: {node_id}")
            return False
        
        # Check if we actually have this contact
        existing_contact = self.get_contact_by_id(node_id)
        if existing_contact:
            self.remove_contact(node_id)
            logger.info(f"👋 Node left DHT: {node_id[:12]}... (reason: {leave_reason})")
            return True
        else:
            logger.debug(f"🤷 Leave notification for unknown node: {node_id[:8]}...")
            return False

    def get_contact_by_id(self, node_id: str) -> Optional['Contact']:
        """Get a contact by node ID"""
        for bucket in self.buckets.values():
            for contact in bucket.contacts:
                if contact.node_id == node_id:
                    return contact
        return None

    def update_contact_from_event(self, node_id: str, new_ip: str, new_port: int) -> bool:
        """Update contact information from network events"""
        existing_contact = self.get_contact_by_id(node_id)
        if existing_contact:
            # Update contact info if it changed
            if existing_contact.ip != new_ip or existing_contact.port != new_port:
                logger.info(f"📍 Contact address updated: {node_id[:8]}... {existing_contact.ip}:{existing_contact.port} -> {new_ip}:{new_port}")
                existing_contact.ip = new_ip
                existing_contact.port = new_port
                existing_contact.last_seen = time.time()
                return True
            else:
                # Just update last_seen
                existing_contact.last_seen = time.time()
                return False
        return False

    def get_routing_table_events(self) -> Dict[str, Any]:
        """Get routing table statistics for event broadcasting"""
        current_time = time.time()
        all_contacts = self.get_all_contacts()
        
        active_contacts = [c for c in all_contacts if current_time - c.last_seen < 60]
        recent_contacts = [c for c in all_contacts if current_time - c.last_seen < 30]
        
        return {
            "total_contacts": len(all_contacts),
            "active_contacts": len(active_contacts),
            "recent_contacts": len(recent_contacts),
            "buckets_count": len(self.buckets),
            "last_updated": current_time
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get routing table statistics"""
        current_time = time.time()
        all_contacts = self.get_all_contacts()
        
        active_contacts = [c for c in all_contacts if current_time - c.last_seen < 30]
        stale_contacts = [c for c in all_contacts if current_time - c.last_seen >= 30]
        
        return {
            "total_contacts": len(all_contacts),
            "active_contacts": len(active_contacts),
            "stale_contacts": len(stale_contacts),
            "buckets_count": len(self.buckets),
            "contact_timeout": self.contact_timeout
        }
