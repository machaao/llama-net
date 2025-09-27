import asyncio
import json
import time
from typing import Dict, Any, Optional, Callable, List, AsyncGenerator
import aiohttp
from common.sse_handler import SSEHandler, SSENetworkMonitor, SSEParser, SSEStreamHandler, OpenAISSETransformer
from common.utils import get_logger

logger = get_logger(__name__)

class UnifiedSSEManager:
    """Unified SSE management combining all SSE functionality"""
    
    def __init__(self, base_url: str = None):
        self.handler = SSEHandler()
        self.monitor = SSENetworkMonitor(base_url) if base_url else None
        self.parser = SSEParser()
        self.stream_handler = SSEStreamHandler()
        self.transformer = OpenAISSETransformer()
        
        if self.monitor:
            self.monitor.set_sse_handler(self.handler)
    
    async def start(self):
        """Start all SSE components"""
        if self.monitor:
            await self.monitor.start()
        self.handler.running = True
        logger.info("Unified SSE manager started")
    
    async def stop(self):
        """Stop all SSE components"""
        if self.monitor:
            await self.monitor.stop()
        self.handler.running = False
        logger.info("Unified SSE manager stopped")
    
    # Delegate methods to appropriate components
    async def add_connection(self, connection_id: str):
        return await self.handler.add_connection(connection_id)
    
    async def remove_connection(self, connection_id: str):
        return await self.handler.remove_connection(connection_id)
    
    async def broadcast_event(self, event_type: str, event_data: Dict[str, Any]):
        return await self.handler.broadcast_event(event_type, event_data)
    
    def get_status(self):
        return self.handler.get_status()
    
    # Stream handling methods
    async def stream_from_response(self, response: aiohttp.ClientResponse, transform_func: Optional[Callable] = None):
        return self.stream_handler.stream_from_response(response, transform_func)
    
    def get_chat_transformer(self):
        return self.transformer.chat_transform
    
    def get_completion_transformer(self):
        return self.transformer.completion_transform
