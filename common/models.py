import json

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union, AsyncGenerator
import time
import uuid

class NodeInfo(BaseModel):
    """Information about an inference node"""
    node_id: str = Field(default_factory=lambda: f"node-{uuid.uuid4().hex[:8]}")
    ip: str
    port: int
    model: str
    load: float = 0.0
    tps: float = 0.0
    uptime: int = 0
    last_seen: int = Field(default_factory=lambda: int(time.time()))
    
    # Additional metadata
    cpu_info: Optional[str] = None
    ram_total: Optional[int] = None
    gpu_info: Optional[str] = None
    context_size: Optional[int] = None

# OpenAI-compatible models only
class OpenAIMessage(BaseModel):
    """OpenAI chat message format"""
    role: str  # "system", "user", "assistant"
    content: str

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

class OpenAIChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request"""
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

class OpenAIChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChoice]
    usage: OpenAIUsage

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

# Streaming OpenAI models
class OpenAIStreamingDelta(BaseModel):
    """OpenAI streaming delta object"""
    content: Optional[str] = None
    role: Optional[str] = None

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
        stream_generator: AsyncGenerator[Dict[str, Any], None]
) -> AsyncGenerator[str, None]:
    """Create OpenAI-compatible streaming chat completion response"""
    created = int(time.time())

    # Send initial chunk with role
    initial_chunk = OpenAIStreamingChatResponse(
        id=request_id,
        created=created,
        model=model,
        choices=[OpenAIStreamingChoice(
            delta=OpenAIStreamingDelta(role="assistant"),
            index=0
        )]
    )
    yield create_sse_data(initial_chunk.dict())

    # Stream content chunks
    async for chunk in stream_generator:
        if chunk.get("text"):
            streaming_chunk = OpenAIStreamingChatResponse(
                id=request_id,
                created=created,
                model=model,
                choices=[OpenAIStreamingChoice(
                    delta=OpenAIStreamingDelta(content=chunk["text"]),
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
        stream_generator: AsyncGenerator[Dict[str, Any], None]
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
                )]
            )
            yield create_sse_data(streaming_chunk.dict())

        if chunk.get("finished"):
            break

    # Send done signal
    yield create_sse_done()