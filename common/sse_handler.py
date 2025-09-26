import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Any, Optional, Callable, List, Set
import aiohttp
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class SSEChunk:
    """Represents a single SSE chunk"""
    data: str
    event: Optional[str] = None
    id: Optional[str] = None
    retry: Optional[int] = None

class SSEHandler:
    """Server-side SSE connection and event management"""
    
    def __init__(self):
        self.active_connections: Dict[str, asyncio.Queue] = {}
        self.event_listeners: List[Callable] = []
        self.running = False
        
    async def add_connection(self, connection_id: str) -> asyncio.Queue:
        """Add a new SSE connection"""
        event_queue = asyncio.Queue(maxsize=100)
        self.active_connections[connection_id] = event_queue
        logger.info(f"SSE connection added: {connection_id} (total: {len(self.active_connections)})")
        return event_queue
    
    async def remove_connection(self, connection_id: str):
        """Remove an SSE connection"""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
            logger.info(f"SSE connection removed: {connection_id} (remaining: {len(self.active_connections)})")
    
    def add_event_listener(self, listener: Callable):
        """Add an event listener function"""
        self.event_listeners.append(listener)
    
    def remove_event_listener(self, listener: Callable):
        """Remove an event listener function"""
        if listener in self.event_listeners:
            self.event_listeners.remove(listener)
    
    async def broadcast_event(self, event_type: str, event_data: Dict[str, Any]):
        """Broadcast an event to all active connections"""
        if not self.active_connections:
            return
        
        event_payload = {
            "type": event_type,
            "timestamp": time.time(),
            **event_data
        }
        
        # Send to all active connections
        disconnected_connections = []
        for connection_id, queue in self.active_connections.items():
            try:
                await asyncio.wait_for(queue.put(event_payload), timeout=1.0)
            except asyncio.TimeoutError:
                logger.warning(f"SSE queue full for connection {connection_id}")
            except Exception as e:
                logger.error(f"Error broadcasting to connection {connection_id}: {e}")
                disconnected_connections.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected_connections:
            await self.remove_connection(connection_id)
        
        # Notify event listeners
        for listener in self.event_listeners:
            try:
                await listener(event_payload)
            except Exception as e:
                logger.error(f"Error in event listener: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get SSE handler status"""
        return {
            "active_connections": len(self.active_connections),
            "event_listeners": len(self.event_listeners),
            "running": self.running,
            "connection_ids": list(self.active_connections.keys())
        }

class SSENetworkMonitor:
    """Monitor network changes and broadcast via SSE"""
    
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.running = False
        self.monitor_task = None
        self.sse_handler = None
        
    async def start(self):
        """Start the network monitor"""
        self.running = True
        # Monitor task can be added here if needed for periodic checks
        logger.info("SSE Network Monitor started")
    
    async def stop(self):
        """Stop the network monitor"""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("SSE Network Monitor stopped")
    
    def set_sse_handler(self, sse_handler: SSEHandler):
        """Set the SSE handler for broadcasting events"""
        self.sse_handler = sse_handler

class SSEParser:
    """Parse Server-Sent Events from a stream"""
    
    def __init__(self):
        self.buffer = ""
    
    def parse_line(self, line: str) -> Optional[SSEChunk]:
        """Parse a single SSE line"""
        line = line.strip()
        
        if not line or line.startswith(':'):
            return None
        
        if line.startswith('data: '):
            data = line[6:]
            if data and data != '[DONE]':
                return SSEChunk(data=data)
        
        return None
    
    def parse_chunk(self, chunk: str) -> List[SSEChunk]:
        """Parse a chunk of data and return SSE events"""
        self.buffer += chunk
        lines = self.buffer.split('\n')
        self.buffer = lines.pop()  # Keep incomplete line
        
        events = []
        for line in lines:
            event = self.parse_line(line)
            if event:
                events.append(event)
        
        return events

class SSEStreamHandler:
    """Handle SSE streaming with robust error handling"""
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.parser = SSEParser()
    
    async def stream_from_response(
        self, 
        response: aiohttp.ClientResponse,
        transform_func: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream and parse SSE data from an aiohttp response"""
        try:
            async for chunk in response.content.iter_any():
                if chunk:
                    chunk_str = chunk.decode('utf-8', errors='ignore')
                    events = self.parser.parse_chunk(chunk_str)
                    
                    for event in events:
                        try:
                            data = json.loads(event.data)
                            
                            # Apply transformation if provided
                            if transform_func:
                                data = transform_func(data)
                            
                            if data:
                                yield data
                                
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse SSE data: {event.data}")
                            continue
                        except Exception as e:
                            logger.error(f"Error processing SSE event: {e}")
                            continue
                            
        except asyncio.CancelledError:
            logger.info("SSE stream cancelled")
            raise
        except aiohttp.ClientConnectionError as e:
            logger.warning(f"SSE connection closed: {e}")
            # Don't re-raise, just end the stream gracefully
        except Exception as e:
            logger.error(f"Unexpected error in SSE stream: {e}")
            # Don't re-raise, just end the stream gracefully

class OpenAISSETransformer:
    """Transform OpenAI SSE format to internal format"""
    
    @staticmethod
    def completion_transform(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Transform OpenAI completion SSE to internal format"""
        if data.get('choices') and len(data['choices']) > 0:
            choice = data['choices'][0]
            if choice.get('text'):
                return {
                    "text": choice['text'],
                    "finished": choice.get('finish_reason') is not None
                }
            elif choice.get('finish_reason'):
                return {"text": "", "finished": True}
        return None
    
    @staticmethod
    def chat_transform(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Transform OpenAI chat SSE to internal format"""
        if data.get('choices') and len(data['choices']) > 0:
            choice = data['choices'][0]
            delta = choice.get('delta', {})
            
            if delta.get('content'):
                return {
                    "text": delta['content'],
                    "finished": choice.get('finish_reason') is not None
                }
            elif choice.get('finish_reason'):
                return {"text": "", "finished": True}
        return None

class SSEForwarder:
    """Forward SSE streams between nodes with error handling"""
    
    def __init__(self, timeout: int = 30):
        self.handler = SSEStreamHandler(timeout)
        self.transformer = OpenAISSETransformer()
    
    async def forward_completion_stream(
        self, 
        url: str, 
        request_data: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Forward a completion stream from another node"""
        
        default_headers = {"Content-Type": "application/json"}
        if headers:
            default_headers.update(headers)
        
        timeout = aiohttp.ClientTimeout(total=self.handler.timeout, connect=5)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=request_data, headers=default_headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Forwarded request failed: {response.status} {error_text}")
                        return
                    
                    async for data in self.handler.stream_from_response(
                        response, 
                        self.transformer.completion_transform
                    ):
                        yield data
                        
        except asyncio.TimeoutError:
            logger.error(f"Timeout forwarding stream to {url}")
        except Exception as e:
            logger.error(f"Error forwarding stream to {url}: {e}")
    
    async def forward_chat_stream(
        self, 
        url: str, 
        request_data: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Forward a chat completion stream from another node"""
        
        default_headers = {"Content-Type": "application/json"}
        if headers:
            default_headers.update(headers)
        
        timeout = aiohttp.ClientTimeout(total=self.handler.timeout, connect=5)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=request_data, headers=default_headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Forwarded chat request failed: {response.status} {error_text}")
                        return
                    
                    async for data in self.handler.stream_from_response(
                        response, 
                        self.transformer.chat_transform
                    ):
                        yield data
                        
        except asyncio.TimeoutError:
            logger.error(f"Timeout forwarding chat stream to {url}")
        except Exception as e:
            logger.error(f"Error forwarding chat stream to {url}: {e}")
