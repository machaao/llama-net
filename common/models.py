import json

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any, Union, AsyncGenerator
import time
import uuid

# Validation constants
_MAX_MESSAGE_COUNT = 50
_MAX_MESSAGE_CHARS = 100_000
_MAX_MAX_TOKENS = 16384

class NodeInfo(BaseModel):
    """Information about an inference node"""
    node_id: str
    model: str
    url: Optional[str] = None  # Tunnel URL (primary address)
    ip: str = ""  # Optional — for identification only
    port: int = 0  # Optional — for identification only
    load: float = 0.0
    tps: float = 0.0
    uptime: int = 0
    last_seen: int = Field(default_factory=lambda: int(time.time()))
    ttft: Optional[float] = None
    latency: Optional[float] = None
    gpu_info: Optional[str] = None
    total_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    context_length: Optional[int] = None

# OpenAI-compatible models with reasoning support
class OpenAIMessage(BaseModel):
    """OpenAI chat message format with reasoning support"""
    role: str
    content: str
    reasoning_content: Optional[str] = None

    @validator('role')
    def validate_role(cls, v):
        allowed = {"system", "user", "assistant", "function", "tool"}
        if v not in allowed:
            raise ValueError(f"Invalid role '{v}'. Must be one of: {', '.join(allowed)}")
        return v

    @validator('content')
    def validate_content_length(cls, v):
        if v is not None and len(v) > _MAX_MESSAGE_CHARS:
            raise ValueError(f"Message content exceeds {_MAX_MESSAGE_CHARS:,} character limit.")
        return v

class OpenAICompletionRequest(BaseModel):
    """OpenAI-compatible completion request"""
    model: str = "llamanet"
    prompt: Union[str, List[str]]
    max_tokens: Optional[int] = Field(default=100, ge=1, le=_MAX_MAX_TOKENS)
    temperature: Optional[float] = Field(default=0.7, ge=0, le=2.0)
    top_p: Optional[float] = Field(default=0.9, ge=0, le=1.0)
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
    conversation_id: Optional[str] = None  # For prefix-aware sticky routing

class OpenAIChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request with reasoning support"""
    model: str = "llamanet"
    messages: List[OpenAIMessage] = Field(..., min_items=1, max_items=_MAX_MESSAGE_COUNT)
    max_tokens: Optional[int] = Field(default=100, ge=1, le=_MAX_MAX_TOKENS)
    temperature: Optional[float] = Field(default=0.7, ge=0, le=2.0)
    top_p: Optional[float] = Field(default=0.9, ge=0, le=1.0)
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    strategy: Optional[str] = "round_robin"
    target_model: Optional[str] = None
    reasoning: Optional[bool] = True  # Enable reasoning by default
    enable_reasoning: Optional[bool] = None  # Alternative parameter name for compatibility
    conversation_id: Optional[str] = None  # For prefix-aware sticky routing

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
    reasoning_content: Optional[str] = None  # Add reasoning_content field

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
        
        # Handle reasoning content first (if available)
        if chunk.get("reasoning_content"):
            delta_content["reasoning_content"] = chunk["reasoning_content"]
        
        # Handle regular content
        if chunk.get("text") or chunk.get("content"):
            delta_content["content"] = chunk.get("text") or chunk.get("content")
        
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
