import asyncio
import json
import time
import uuid
from typing import Dict, Any
from common.p2p_transport import P2PTransport
from common.models import (
    OpenAICompletionRequest, OpenAIChatCompletionRequest,
    OpenAICompletionResponse, OpenAIChatCompletionResponse,
    OpenAIChoice, OpenAIUsage, OpenAIMessage
)
from inference_node.llm_wrapper import LlamaWrapper
from inference_node.config import InferenceConfig
from common.utils import get_logger, get_host_ip

logger = get_logger(__name__)

class P2PRequestHandler:
    """Handle inference requests via P2P transport"""
    
    def __init__(self, config: InferenceConfig, llm: LlamaWrapper):
        self.config = config
        self.llm = llm
        self.transport = P2PTransport(config.node_id, config.model_name)
        
    async def start(self):
        """Start P2P request handler with error handling"""
        try:
            await self.transport.start()
            self.transport.add_message_callback(self._handle_p2p_request)
            logger.info(f"P2P request handler started")
        except ImportError as e:
            logger.warning(f"P2P dependencies not available: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to start P2P transport: {e}")
            raise
        
    async def _handle_p2p_request(self, msg: bytes, client_tup, pipe):
        """Handle incoming P2P inference requests"""
        try:
            # Parse request
            request_data = json.loads(msg.decode('utf-8'))
            request_type = request_data.get('type')
            
            if request_type == 'completion_request':
                await self._handle_completion_request(request_data, pipe)
            elif request_type == 'chat_completion_request':
                await self._handle_chat_completion_request(request_data, pipe)
            elif request_type == 'status_request':
                await self._handle_status_request(request_data, pipe)
            elif request_type == 'ping':
                await self._handle_ping_request(request_data, pipe)
            else:
                logger.warning(f"Unknown P2P request type: {request_type}")
                
        except Exception as e:
            logger.error(f"Error handling P2P request: {e}")
            # Send error response
            error_response = {
                'type': 'error_response',
                'error': str(e),
                'timestamp': time.time()
            }
            try:
                await self.transport.send_message(pipe, json.dumps(error_response).encode('utf-8'))
            except:
                pass
                
    async def _handle_completion_request(self, request_data: Dict[str, Any], pipe):
        """Handle completion request via P2P"""
        try:
            request = OpenAICompletionRequest(**request_data['data'])
            
            # Handle prompt (can be string or list)
            if isinstance(request.prompt, list):
                prompt = request.prompt[0] if request.prompt else ""
            else:
                prompt = request.prompt
            
            # Normalize stop tokens
            stop_tokens = None
            if request.stop:
                if isinstance(request.stop, str):
                    stop_tokens = [request.stop] if request.stop.strip() else None
                elif isinstance(request.stop, list):
                    stop_tokens = [str(token).strip() for token in request.stop if str(token).strip()]
                    stop_tokens = stop_tokens if stop_tokens else None
            
            # Generate response
            result = self.llm.generate(
                prompt=prompt,
                max_tokens=request.max_tokens or 100,
                temperature=request.temperature or 0.7,
                top_p=request.top_p or 0.9,
                stop=stop_tokens,
                repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
            )
            
            # Calculate token counts
            prompt_tokens = len(prompt.split())
            completion_tokens = result["tokens_generated"]
            
            # Create response
            choice = OpenAIChoice(
                text=result["text"],
                index=0,
                finish_reason="stop"
            )
            
            usage = OpenAIUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens
            )
            
            response = OpenAICompletionResponse(
                id=f"cmpl-{uuid.uuid4().hex[:8]}",
                created=int(time.time()),
                model=request.model,
                choices=[choice],
                usage=usage,
                node_info={
                    "node_id": self.config.node_id,
                    "ip": get_host_ip(),
                    "port": self.config.port,
                    "model": self.config.model_name,
                    "processing_node": "p2p",
                    "transport": "p2p"
                }
            )
            
            # Send response
            response_msg = {
                'type': 'completion_response',
                'data': response.dict(),
                'request_id': request_data.get('request_id'),
                'timestamp': time.time()
            }
            
            await self.transport.send_message(pipe, json.dumps(response_msg).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error in P2P completion request: {e}")
            raise
            
    async def _handle_chat_completion_request(self, request_data: Dict[str, Any], pipe):
        """Handle chat completion request via P2P"""
        try:
            request = OpenAIChatCompletionRequest(**request_data['data'])
            
            # Convert messages to prompt
            prompt_parts = []
            for message in request.messages:
                if message.role == "system":
                    prompt_parts.append(f"System: {message.content}")
                elif message.role == "user":
                    prompt_parts.append(f"Human: {message.content}")
                elif message.role == "assistant":
                    prompt_parts.append(f"Assistant: {message.content}")
            
            prompt = "\n\n".join(prompt_parts) + "\n\nAssistant:"
            
            # Default stop tokens for chat format
            stop_tokens = ["\n\nHuman:", "\n\nUser:", "\nHuman:", "\nUser:", "Human:", "User:"]
            if request.stop:
                if isinstance(request.stop, str):
                    stop_tokens.append(request.stop)
                elif isinstance(request.stop, list):
                    stop_tokens.extend(request.stop)
            
            # Generate response
            result = self.llm.generate(
                prompt=prompt,
                max_tokens=request.max_tokens or 100,
                temperature=request.temperature or 0.7,
                top_p=request.top_p or 0.9,
                stop=stop_tokens,
                repeat_penalty=1.0 + (request.frequency_penalty or 0.0)
            )
            
            # Calculate token counts
            prompt_tokens = len(prompt.split())
            completion_tokens = result["tokens_generated"]
            
            # Create response
            response_message = OpenAIMessage(
                role="assistant",
                content=result["text"].strip()
            )
            
            choice = OpenAIChoice(
                message=response_message,
                index=0,
                finish_reason="stop"
            )
            
            usage = OpenAIUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens
            )
            
            response = OpenAIChatCompletionResponse(
                id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
                created=int(time.time()),
                model=request.model,
                choices=[choice],
                usage=usage,
                node_info={
                    "node_id": self.config.node_id,
                    "ip": get_host_ip(),
                    "port": self.config.port,
                    "model": self.config.model_name,
                    "processing_node": "p2p",
                    "transport": "p2p"
                }
            )
            
            # Send response
            response_msg = {
                'type': 'chat_completion_response',
                'data': response.dict(),
                'request_id': request_data.get('request_id'),
                'timestamp': time.time()
            }
            
            await self.transport.send_message(pipe, json.dumps(response_msg).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error in P2P chat completion request: {e}")
            raise
            
    async def _handle_status_request(self, request_data: Dict[str, Any], pipe):
        """Handle status request via P2P"""
        try:
            metrics = self.llm.get_metrics()
            
            response_msg = {
                'type': 'status_response',
                'data': {
                    **metrics,
                    'node_id': self.config.node_id,
                    'model': self.config.model_name,
                    'transport': 'p2p',
                    'p2p_info': self.transport.get_address_info()
                },
                'request_id': request_data.get('request_id'),
                'timestamp': time.time()
            }
            
            await self.transport.send_message(pipe, json.dumps(response_msg).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error in P2P status request: {e}")
            raise
            
    async def _handle_ping_request(self, request_data: Dict[str, Any], pipe):
        """Handle ping request via P2P"""
        try:
            response_msg = {
                'type': 'pong_response',
                'data': {
                    'node_id': self.config.node_id,
                    'model': self.config.model_name,
                    'timestamp': time.time()
                },
                'request_id': request_data.get('request_id'),
                'timestamp': time.time()
            }
            
            await self.transport.send_message(pipe, json.dumps(response_msg).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error in P2P ping request: {e}")
            raise
            
    def get_p2p_info(self) -> Dict[str, Any]:
        """Get P2P transport information"""
        return self.transport.get_address_info()
        
    async def close(self):
        """Close P2P request handler"""
        await self.transport.close()
