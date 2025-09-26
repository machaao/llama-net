import asyncio
import json
import struct
import time
import hashlib
import uuid
from typing import Dict, Any, Tuple, Optional
from common.utils import get_logger

logger = get_logger(__name__)

class KademliaProtocol(asyncio.DatagramProtocol):
    """UDP protocol for Kademlia messages"""
    
    def __init__(self, node):
        self.node = node
        self.transport = None
        self.pending_requests: Dict[str, asyncio.Future] = {}
    
    def connection_made(self, transport):
        self.transport = transport
    
    def error_received(self, exc):
        """Handle transport errors gracefully"""
        logger.error(f"UDP transport error: {exc}")
        
        # Handle specific error types without crashing
        error_str = str(exc).lower()
        if "connection refused" in error_str:
            logger.warning("Connection refused - remote node may be down")
        elif "network unreachable" in error_str:
            logger.warning("Network unreachable - check connectivity")
        elif "no buffer space available" in error_str:
            logger.warning("Network buffer full - system under load")
        elif "address already in use" in error_str:
            logger.error("Port already in use - check for conflicting processes")
        else:
            logger.error(f"Unhandled transport error: {exc}")

    def connection_lost(self, exc):
        """Handle connection loss"""
        if exc:
            logger.error(f"UDP connection lost: {exc}")
        else:
            logger.debug("UDP connection closed normally")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming UDP messages with robust error handling"""
        try:
            # Validate data
            if not data:
                logger.debug(f"Received empty UDP packet from {addr}")
                return
                
            if len(data) > 65507:  # Max UDP payload size
                logger.warning(f"Received oversized UDP packet ({len(data)} bytes) from {addr}")
                return
            
            # Decode with proper error handling
            try:
                message_str = data.decode('utf-8')
            except UnicodeDecodeError as e:
                logger.warning(f"Failed to decode message from {addr}: {e}")
                return
                
            # Parse JSON with validation
            try:
                message = json.loads(message_str)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from {addr}: {e}")
                return
                
            # Validate message structure
            if not isinstance(message, dict) or 'type' not in message:
                logger.warning(f"Invalid message structure from {addr}")
                return
                
            # Handle message asynchronously with error protection
            asyncio.create_task(self._handle_message_safe(message, addr))
            
        except Exception as e:
            logger.error(f"Unexpected error in datagram_received from {addr}: {e}")
            # Don't re-raise - this would crash the event loop

    async def _handle_message_safe(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Safely handle message with error isolation"""
        try:
            await self._handle_message(message, addr)
        except Exception as e:
            logger.error(f"Error handling message from {addr}: {e}")
            # Log but don't crash the protocol handler
    
    async def _handle_message(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Enhanced message handling with join/leave events"""
        msg_type = message.get('type')
        sender_id = message.get('sender_id')
        
        # Track if this is a new contact
        is_new_contact = False
        
        # Update contact activity for any message (except responses to avoid loops)
        if sender_id and msg_type != 'response':
            from dht.kademlia_node import Contact
            
            # Check if this is a new contact
            existing_contacts = self.node.routing_table.get_all_contacts()
            existing_ids = {c.node_id for c in existing_contacts}
            is_new_contact = sender_id not in existing_ids
            
            contact = Contact(sender_id, addr[0], addr[1])
            self.node.routing_table.add_contact(contact)
            
            # Emit join event for new contacts
            if is_new_contact:
                await self._emit_contact_joined_event(contact)
            
            logger.debug(f"📡 Updated contact activity: {sender_id[:8]}... from {addr}")
        
        # Handle specific message types
        if msg_type == 'ping':
            await self._handle_ping_enhanced(message, addr, is_new_contact)
        elif msg_type == 'store':
            await self._handle_store(message, addr)
        elif msg_type == 'find_node':
            await self._handle_find_node(message, addr)
        elif msg_type == 'find_value':
            await self._handle_find_value(message, addr)
        elif msg_type == 'response':
            await self._handle_response(message, addr)
        elif msg_type == 'join_notification':
            await self._handle_join_notification(message, addr)
        elif msg_type == 'leave_notification':
            await self._handle_leave_notification(message, addr)
        else:
            logger.warning(f"Unknown message type: {msg_type}")
    
    async def _handle_ping(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle ping message"""
        sender_id = message.get('sender_id')
        if sender_id:
            from dht.kademlia_node import Contact
            contact = Contact(sender_id, addr[0], addr[1])
            self.node.routing_table.add_contact(contact)
        
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {
                'pong': True,
                'sender_id': self.node.node_id
            }
        }
        await self._send_message(response, addr)

    async def _handle_ping_enhanced(self, message: Dict[str, Any], addr: Tuple[str, int], is_new_contact: bool):
        """Enhanced ping handling with join detection"""
        sender_id = message.get('sender_id')
        
        if sender_id:
            from dht.kademlia_node import Contact
            contact = Contact(sender_id, addr[0], addr[1])
            self.node.routing_table.add_contact(contact)
            
            # If this is a new contact, it might be joining
            if is_new_contact:
                logger.info(f"🆕 New node detected via ping: {sender_id[:8]}... from {addr}")
        
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {
                'pong': True,
                'sender_id': self.node.node_id,
                'node_info': {
                    'node_id': self.node.node_id,
                    'timestamp': time.time(),
                    'is_new_contact_response': is_new_contact
                }
            }
        }
        await self._send_message(response, addr)

    async def _emit_contact_joined_event(self, contact):
        """Emit event when a new contact joins the DHT"""
        try:
            # Try to get the event publisher to emit the event
            if hasattr(self.node, 'event_publisher'):
                await self.node.event_publisher._broadcast_node_event("dht_contact_joined", {
                    'contact_node_id': contact.node_id,
                    'contact_ip': contact.ip,
                    'contact_port': contact.port,
                    'local_node_id': self.node.node_id,
                    'join_method': 'dht_protocol'
                })
        except Exception as e:
            logger.debug(f"Could not emit contact joined event: {e}")

    async def _handle_join_notification(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle explicit join notifications"""
        sender_id = message.get('sender_id')
        join_data = message.get('join_data', {})
        
        logger.info(f"📥 Received join notification from {sender_id[:8]}... at {addr}")
        
        # Add to routing table
        if sender_id:
            from dht.kademlia_node import Contact
            contact = Contact(sender_id, addr[0], addr[1])
            self.node.routing_table.add_contact(contact)
        
        # Acknowledge the join
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {
                'join_acknowledged': True,
                'welcomer_node_id': self.node.node_id
            }
        }
        await self._send_message(response, addr)

    async def _handle_leave_notification(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle explicit leave notifications"""
        sender_id = message.get('sender_id')
        leave_data = message.get('leave_data', {})
        
        logger.info(f"📤 Received leave notification from {sender_id[:8]}... at {addr}")
        
        # Remove from routing table
        if sender_id:
            self.node.routing_table.remove_contact(sender_id)
        
        # Acknowledge the leave
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {
                'leave_acknowledged': True,
                'acknowledger_node_id': self.node.node_id
            }
        }
        await self._send_message(response, addr)
    
    async def _handle_store(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle store message"""
        key = message.get('key')
        value = message.get('value')
        
        if key and value is not None:
            self.node.storage[key] = {
                'value': value,
                'timestamp': time.time()
            }
        
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {'stored': True}
        }
        await self._send_message(response, addr)
    
    async def _handle_find_node(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle find_node message"""
        target_id = message.get('target_id')
        closest = self.node.routing_table.find_closest_contacts(target_id, self.node.k)
        
        contacts_data = [
            {'node_id': c.node_id, 'ip': c.ip, 'port': c.port}
            for c in closest
        ]
        
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {'contacts': contacts_data}
        }
        await self._send_message(response, addr)
    
    async def _handle_find_value(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle find_value message"""
        key = message.get('key')
        
        # Check if we have the value
        if key in self.node.storage:
            stored_item = self.node.storage[key]
            if time.time() - stored_item['timestamp'] < self.node.ttl:
                response = {
                    'type': 'response',
                    'id': message.get('id'),
                    'sender_id': self.node.node_id,
                    'data': {'value': stored_item['value']}
                }
                await self._send_message(response, addr)
                return
        
        # Return closest nodes instead
        target_hash = hashlib.sha1(key.encode()).hexdigest()
        closest = self.node.routing_table.find_closest_contacts(target_hash, self.node.k)
        
        contacts_data = [
            {'node_id': c.node_id, 'ip': c.ip, 'port': c.port}
            for c in closest
        ]
        
        response = {
            'type': 'response',
            'id': message.get('id'),
            'sender_id': self.node.node_id,
            'data': {'contacts': contacts_data}
        }
        await self._send_message(response, addr)
    
    async def _handle_response(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle response message"""
        msg_id = message.get('id')
        sender_id = message.get('sender_id')
        
        # Update contact activity for responses too
        if sender_id:
            self.node.routing_table.update_contact_seen(sender_id)
        
        if msg_id in self.pending_requests:
            future = self.pending_requests.pop(msg_id)
            if not future.done():
                future.set_result(message.get('data'))
    
    async def _send_message(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Send a message to an address"""
        try:
            data = json.dumps(message).encode()
            self.transport.sendto(data, addr)
        except Exception as e:
            logger.error(f"Failed to send message to {addr}: {e}")
    
    async def send_request(self, message: Dict[str, Any], addr: Tuple[str, int], timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """Send a request and wait for response"""
        msg_id = message['id']
        future = asyncio.Future()
        self.pending_requests[msg_id] = future
        
        await self._send_message(message, addr)
        
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self.pending_requests.pop(msg_id, None)
            return None
