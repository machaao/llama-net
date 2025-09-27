from client.event_aware_client import EventAwareOpenAIClient
from common.utils import get_logger

logger = get_logger(__name__)

# Simplified API with consolidated client
OpenAIClient = EventAwareOpenAIClient
Client = EventAwareOpenAIClient
