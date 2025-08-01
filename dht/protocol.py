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
    
    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming UDP messages"""
        try:
            message = json.loads(data.decode())
            asyncio.create_task(self._handle_message(message, addr))
        except Exception as e:
            logger.warning(f"Failed to parse message from {addr}: {e}")
    
    async def _handle_message(self, message: Dict[str, Any], addr: Tuple[str, int]):
        """Handle a parsed message"""
        msg_type = message.get('type')
        msg_id = message.get('id')
        
        if msg_type == 'ping':
            await self._handle_ping(message, addr)
        elif msg_type == 'store':
            await self._handle_store(message, addr)
        elif msg_type == 'find_node':
            await self._handle_find_node(message, addr)
        elif msg_type == 'find_value':
            await self._handle_find_value(message, addr)
        elif msg_type == 'response':
            await self._handle_response(message, addr)
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
            'data': {'pong': True}
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
