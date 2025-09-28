import json

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union, AsyncGenerator
import time
import uuid

class NodeInfo(BaseModel):
    """Information about an inference node"""
    node_id: str
    ip: str  # Primary IP for backward compatibility
    port: int
    model: str
    load: float = 0.0
    tps: float = 0.0
    uptime: int = 0
    last_seen: int = Field(default_factory=lambda: int(time.time()))
    
    # Event-driven metadata
    event_driven: bool = True  # Mark as event-driven update
    last_significant_change: Optional[int] = None  # When metrics last changed significantly
    change_reason: Optional[str] = None  # Why this update was triggered
    
    # Multi-IP support for auto IP selection
    available_ips: Optional[List[str]] = None  # All available IP addresses
    ip_types: Optional[Dict[str, str]] = None  # IP classification (public/private/loopback)
    preferred_ip: Optional[str] = None  # Client's preferred IP after testing
    
    # Additional metadata
    cpu_info: Optional[str] = None
    ram_total: Optional[int] = None
    gpu_info: Optional[str] = None
    context_size: Optional[int] = None
    
    def get_all_ips(self) -> List[str]:
        """Get all available IP addresses for this node"""
        if self.available_ips:
            return self.available_ips
        return [self.ip]  # Fallback to primary IP
    
    def get_best_ip_for_client(self, client_ip: str = None) -> str:
        """Get the best IP address for a client to connect to"""
        if self.preferred_ip:
            return self.preferred_ip
        
        # If we have multiple IPs, try to select the best one
        if self.available_ips and len(self.available_ips) > 1:
            # Prefer public IPs, then private, then others
            public_ips = [ip for ip in self.available_ips 
                         if self.ip_types and self.ip_types.get(ip) == "public"]
            private_ips = [ip for ip in self.available_ips 
                          if self.ip_types and self.ip_types.get(ip) == "private"]
            
            if public_ips:
                return public_ips[0]
            elif private_ips:
                return private_ips[0]
        
        return self.ip  # Fallback to primary IP

# OpenAI-compatible models with reasoning support
class OpenAIMessage(BaseModel):
    """OpenAI chat message format with reasoning support"""
    role: str  # "system", "user", "assistant"
    content: str
    reasoning: Optional[str] = None  # Add reasoning field for reasoning models

class OpenAICompletionRequest(BaseModel):
    """OpenAI-compatible completion request"""
    model: str = "llamanet"
    prompt: Union[str, List[str]]
    max_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    suffix: Optional[str] = None
    echo: Optional[bool] = False
    strategy: Optional[str] = "round_robin"
    target_model: Optional[str] = None  # Add target model parameter
    reasoning: Optional[bool] = True  # Add reasoning parameter

class OpenAIChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request with reasoning support"""
    model: str = "llamanet"
    messages: List[OpenAIMessage]
    max_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    strategy: Optional[str] = "round_robin"
    target_model: Optional[str] = None
    reasoning: Optional[bool] = True  # Add reasoning parameter

class OpenAIChoice(BaseModel):
    """OpenAI choice object"""
    text: Optional[str] = None
    message: Optional[OpenAIMessage] = None
    index: int
    finish_reason: Optional[str] = "stop"
    logprobs: Optional[Dict] = None

class OpenAIUsage(BaseModel):
    """OpenAI usage statistics"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class OpenAICompletionResponse(BaseModel):
    """OpenAI-compatible completion response"""
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[OpenAIChoice]
    usage: OpenAIUsage
    node_info: Optional[Dict[str, Any]] = None

class OpenAIChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChoice]
    usage: OpenAIUsage
    node_info: Optional[Dict[str, Any]] = None

class OpenAIModel(BaseModel):
    """OpenAI model object"""
    id: str
    object: str = "model"
    created: int
    owned_by: str = "llamanet"

class OpenAIModelList(BaseModel):
    """OpenAI models list response"""
    object: str = "list"
    data: List[OpenAIModel]

# Streaming OpenAI models with reasoning support
class OpenAIStreamingDelta(BaseModel):
    """OpenAI streaming delta object with reasoning support"""
    content: Optional[str] = None
    role: Optional[str] = None
    reasoning: Optional[str] = None  # Add reasoning field

class OpenAIStreamingChoice(BaseModel):
    """OpenAI streaming choice object"""
    delta: OpenAIStreamingDelta
    index: int
    finish_reason: Optional[str] = None

class OpenAIStreamingChatResponse(BaseModel):
    """OpenAI-compatible streaming chat response"""
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[OpenAIStreamingChoice]
    node_info: Optional[Dict[str, Any]] = None

class OpenAIStreamingCompletionChoice(BaseModel):
    """OpenAI streaming completion choice"""
    text: str
    index: int
    finish_reason: Optional[str] = None
    logprobs: Optional[Dict] = None

class OpenAIStreamingCompletionResponse(BaseModel):
    """OpenAI-compatible streaming completion response"""
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[OpenAIStreamingCompletionChoice]
    node_info: Optional[Dict[str, Any]] = None


# Streaming utilities
def create_sse_data(data: Dict[str, Any]) -> str:
    """Create Server-Sent Events formatted data"""
    return f"data: {json.dumps(data)}\n\n"


def create_sse_done() -> str:
    """Create SSE done signal"""
    return "data: [DONE]\n\n"


async def create_streaming_chat_response(
        request_id: str,
        model: str,
        stream_generator: AsyncGenerator[Dict[str, Any], None],
        node_info: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    """Create OpenAI-compatible streaming chat completion response with reasoning support"""
    created = int(time.time())

    # Send initial chunk with role and node info
    initial_chunk = OpenAIStreamingChatResponse(
        id=request_id,
        created=created,
        model=model,
        choices=[OpenAIStreamingChoice(
            delta=OpenAIStreamingDelta(role="assistant"),
            index=0
        )],
        node_info=node_info
    )
    yield create_sse_data(initial_chunk.dict())

    # Stream content chunks with reasoning support
    async for chunk in stream_generator:
        delta_content = {}
        
        if chunk.get("text"):
            delta_content["content"] = chunk["text"]
        
        if chunk.get("reasoning"):
            delta_content["reasoning"] = chunk["reasoning"]
        
        if delta_content:
            streaming_chunk = OpenAIStreamingChatResponse(
                id=request_id,
                created=created,
                model=model,
                choices=[OpenAIStreamingChoice(
                    delta=OpenAIStreamingDelta(**delta_content),
                    index=0,
                    finish_reason=None if not chunk.get("finished") else "stop"
                )]
            )
            yield create_sse_data(streaming_chunk.dict())

        if chunk.get("finished"):
            # Send final chunk with finish_reason
            final_chunk = OpenAIStreamingChatResponse(
                id=request_id,
                created=created,
                model=model,
                choices=[OpenAIStreamingChoice(
                    delta=OpenAIStreamingDelta(),
                    index=0,
                    finish_reason="stop"
                )]
            )
            yield create_sse_data(final_chunk.dict())
            break

    # Send done signal
    yield create_sse_done()


async def create_streaming_completion_response(
        request_id: str,
        model: str,
        stream_generator: AsyncGenerator[Dict[str, Any], None],
        node_info: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    """Create OpenAI-compatible streaming completion response"""
    created = int(time.time())

    # Stream content chunks
    async for chunk in stream_generator:
        if chunk.get("text"):
            streaming_chunk = OpenAIStreamingCompletionResponse(
                id=request_id,
                created=created,
                model=model,
                choices=[OpenAIStreamingCompletionChoice(
                    text=chunk["text"],
                    index=0,
                    finish_reason=None if not chunk.get("finished") else "stop"
                )],
                node_info=node_info if chunk.get("text") else None  # Include node_info in first content chunk
            )
            yield create_sse_data(streaming_chunk.dict())

        if chunk.get("finished"):
            break

    # Send done signal
    yield create_sse_done()
