from typing import Dict, Any, Tuple, Optional, TYPE_CHECKING
from common.utils import get_logger

if TYPE_CHECKING:
    from dht.protocol import KademliaProtocol
    from dht.http_transport import HTTPDHTTransport
    from dht.kademlia_node import Contact

logger = get_logger(__name__)


class TransportRouter:
    """Routes DHT messages via UDP or HTTP depending on the peer's transport"""

    def __init__(self, udp_protocol: "KademliaProtocol", http_transport: "HTTPDHTTransport"):
        self.udp = udp_protocol
        self.http = http_transport

    async def send_request(
        self, message: Dict[str, Any], contact: "Contact", timeout: float = 5.0
    ) -> Optional[Dict[str, Any]]:
        """Send a DHT request to a contact using the appropriate transport"""
        addr = (contact.ip, contact.port)

        if contact.transport == "http" and contact.http_url:
            logger.debug(
                f"Sending DHT {message.get('type')} via HTTP to "
                f"{contact.node_id[:8]}... ({contact.http_url})"
            )
            self.http.register_peer_url(
                contact.ip, contact.port, contact.http_url
            )
            return await self.http.send_request(message, addr, timeout)
        else:
            logger.debug(
                f"Sending DHT {message.get('type')} via UDP to "
                f"{contact.node_id[:8]}... ({contact.ip}:{contact.port})"
            )
            return await self.udp.send_request(message, addr, timeout)
