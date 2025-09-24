import asyncio
import json
import logging
from typing import AsyncGenerator, Dict, Any, Optional, Callable, List
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
