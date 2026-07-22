from typing import Dict, Any
from common.sse_handler import SSEHandler
from common.utils import get_logger

logger = get_logger(__name__)

class UnifiedSSEManager:
    """SSE manager wrapping SSEHandler for connection management and broadcasting."""

    def __init__(self, base_url: str = None):
        self.handler = SSEHandler()

    async def start(self):
        self.handler.running = True

    async def stop(self):
        self.handler.running = False

    async def add_connection(self, connection_id: str):
        return await self.handler.add_connection(connection_id)

    async def remove_connection(self, connection_id: str):
        return await self.handler.remove_connection(connection_id)

    async def broadcast_event(self, event_type: str, event_data: Dict[str, Any]):
        return await self.handler.broadcast_event(event_type, event_data)

    def get_status(self):
        return self.handler.get_status()
