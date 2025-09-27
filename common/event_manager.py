import asyncio
import time
from typing import Dict, Any, List, Callable, Optional
from dataclasses import dataclass
from enum import Enum
from common.utils import get_logger

logger = get_logger(__name__)

class EventType(Enum):
    NODE_JOINED = "node_joined"
    NODE_LEFT = "node_left"
    NODE_UPDATED = "node_updated"
    NETWORK_CHANGED = "network_changed"
    BOOTSTRAP_CONNECTED = "bootstrap_connected"
    DHT_CONTACT_JOINED = "dht_contact_joined"
    BOOTSTRAP_COMPLETED = "bootstrap_completed"

@dataclass
class Event:
    event_type: EventType
    data: Dict[str, Any]
    timestamp: float
    source: str = "unknown"

class EventManager:
    """Centralized event management for all components"""
    
    def __init__(self):
        self.listeners: Dict[EventType, List[Callable]] = {}
        self.event_queue = asyncio.Queue()
        self.running = False
        self.processor_task = None
        self.event_history: List[Event] = []
        self.max_history = 1000
    
    def subscribe(self, event_type: EventType, callback: Callable):
        """Subscribe to events"""
        if event_type not in self.listeners:
            self.listeners[event_type] = []
        self.listeners[event_type].append(callback)
        logger.debug(f"Subscribed to {event_type.value} events")
    
    def unsubscribe(self, event_type: EventType, callback: Callable):
        """Unsubscribe from events"""
        if event_type in self.listeners and callback in self.listeners[event_type]:
            self.listeners[event_type].remove(callback)
            logger.debug(f"Unsubscribed from {event_type.value} events")
    
    async def emit(self, event: Event):
        """Emit an event"""
        # Add to history
        self.event_history.append(event)
        if len(self.event_history) > self.max_history:
            self.event_history = self.event_history[-self.max_history:]
        
        # Queue for processing
        await self.event_queue.put(event)
        
        # Also notify direct listeners
        if event.event_type in self.listeners:
            for callback in self.listeners[event.event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event)
                    else:
                        callback(event)
                except Exception as e:
                    logger.error(f"Error in event listener: {e}")
    
    async def start(self):
        """Start event processing"""
        if self.running:
            return
        
        self.running = True
        self.processor_task = asyncio.create_task(self._process_events())
        logger.info("Event manager started")
    
    async def stop(self):
        """Stop event processing"""
        self.running = False
        if self.processor_task:
            self.processor_task.cancel()
            try:
                await self.processor_task
            except asyncio.CancelledError:
                pass
        logger.info("Event manager stopped")
    
    async def _process_events(self):
        """Process events from queue"""
        while self.running:
            try:
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
                logger.debug(f"Processing event: {event.event_type.value} from {event.source}")
                
                # Additional event processing logic can be added here
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing event: {e}")
    
    def get_event_history(self, event_type: Optional[EventType] = None, limit: int = 100) -> List[Event]:
        """Get event history, optionally filtered by type"""
        if event_type:
            filtered = [e for e in self.event_history if e.event_type == event_type]
            return filtered[-limit:]
        return self.event_history[-limit:]
    
    def get_event_stats(self) -> Dict[str, Any]:
        """Get event statistics"""
        stats = {}
        for event_type in EventType:
            count = len([e for e in self.event_history if e.event_type == event_type])
            stats[event_type.value] = count
        
        return {
            "total_events": len(self.event_history),
            "event_counts": stats,
            "active_listeners": sum(len(listeners) for listeners in self.listeners.values()),
            "running": self.running
        }

# Global event manager instance
_event_manager: Optional[EventManager] = None

def get_event_manager() -> EventManager:
    """Get the global event manager instance"""
    global _event_manager
    if _event_manager is None:
        _event_manager = EventManager()
    return _event_manager
