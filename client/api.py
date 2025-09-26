import asyncio
import requests
import json
from typing import Optional, Dict, Any, List
from common.models import (
    OpenAIChatCompletionRequest, OpenAICompletionRequest,
    OpenAIChatCompletionResponse, OpenAICompletionResponse,
    OpenAIMessage, NodeInfo
)
from client.event_aware_client import EventAwareOpenAIClient
from common.utils import get_logger

logger = get_logger(__name__)

# For backward compatibility, alias the event-aware client as OpenAIClient
OpenAIClient = EventAwareOpenAIClient

# Legacy OpenAIClient implementation for reference (now using event-based discovery)
class LegacyOpenAIClient:
    """Legacy OpenAI-compatible client - replaced by EventAwareOpenAIClient"""
    
    def __init__(self, *args, **kwargs):
        logger.warning("LegacyOpenAIClient is deprecated. Use EventAwareOpenAIClient instead.")
        # Redirect to the new implementation
        self._client = EventAwareOpenAIClient(*args, **kwargs)
    
    def __getattr__(self, name):
        return getattr(self._client, name)

# Backward compatibility alias
Client = OpenAIClient
