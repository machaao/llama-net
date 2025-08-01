from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
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

class GenerationRequest(BaseModel):
    """Request for text generation"""
    prompt: str
    max_tokens: int = 100
    temperature: float = 0.7
    top_p: float = 0.9
    top_k: int = 40
    stop: Optional[List[str]] = None
    repeat_penalty: float = 1.1
    
class GenerationResponse(BaseModel):
    """Response from text generation"""
    text: str
    tokens_generated: int
    generation_time: float
    node_id: str
