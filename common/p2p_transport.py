import asyncio
import json
import time
from typing import Optional, Dict, Any, Callable, List
from p2pd import P2PNode
from common.utils import get_logger

logger = get_logger(__name__)

class P2PTransport:
    """P2PD transport layer for NAT traversal"""
    
    def __init__(self, node_id: str, model_name: str = None):
        self.node_id = node_id
        self.model_name = model_name
        self.p2p_node: Optional[P2PNode] = None
        self.message_callbacks = []
        self.nickname = None
        self.running = False
        
    async def start(self, port: int = None):
        """Start P2P node"""
        try:
            self.p2p_node = await P2PNode()
            self.p2p_node.add_msg_cb(self._handle_message)
            
            # Create unique nickname based on model and node_id
            if self.model_name:
                self.nickname = f"{self.model_name}-{self.node_id[:8]}"
            else:
                self.nickname = f"client-{self.node_id[:8]}"
            
            # Note: p2pd library doesn't support nickname registration
            # Nickname is used for identification in connection attempts
            logger.info(f"P2P node started with identifier: {self.nickname}")
                
            self.running = True
            logger.info(f"P2P transport started for node: {self.node_id}")
            
        except Exception as e:
            logger.error(f"Failed to start P2P transport: {e}")
            raise
            
    async def connect_to_peer(self, peer_nickname: str, timeout: float = 10.0) -> Optional[Any]:
        """Connect to a peer using P2PD with timeout"""
        if not self.p2p_node:
            raise RuntimeError("P2P node not started")
            
        try:
            # Try to connect with timeout
            # Note: p2pd may require peer ID instead of nickname
            pipe = await asyncio.wait_for(
                self.p2p_node.connect(peer_nickname),
                timeout=timeout
            )
            logger.info(f"P2P connected to peer: {peer_nickname}")
            return pipe
        except asyncio.TimeoutError:
            logger.warning(f"P2P connection timeout to peer: {peer_nickname}")
            return None
        except AttributeError as e:
            logger.error(f"P2P method not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to connect to peer {peer_nickname}: {e}")
            return None
            
    async def send_message(self, pipe, message: bytes):
        """Send message through P2P pipe"""
        try:
            await pipe.send(message)
        except Exception as e:
            logger.error(f"Failed to send P2P message: {e}")
            raise
            
    def add_message_callback(self, callback: Callable):
        """Add callback for incoming messages"""
        self.message_callbacks.append(callback)
        
    async def _handle_message(self, msg: bytes, client_tup, pipe):
        """Handle incoming P2P messages"""
        for callback in self.message_callbacks:
            try:
                await callback(msg, client_tup, pipe)
            except Exception as e:
                logger.error(f"Error in P2P message callback: {e}")
                
    def get_address_info(self) -> Dict[str, Any]:
        """Get P2P address information"""
        if not self.p2p_node:
            return {}
            
        return {
            "nickname": self.nickname,
            "p2p_enabled": True,
            "supports_nat_traversal": True
        }
                
    async def close(self):
        """Close P2P transport"""
        self.running = False
        if self.p2p_node:
            try:
                await self.p2p_node.close()
                logger.info("P2P transport closed")
            except Exception as e:
                logger.error(f"Error closing P2P transport: {e}")
