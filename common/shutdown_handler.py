import asyncio
import time
import signal
import logging
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class ShutdownPhase(Enum):
    """Phases of graceful shutdown"""
    INITIATED = "initiated"
    STOPPING_SERVICES = "stopping_services"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class ShutdownTask:
    """Represents a shutdown task with timeout and priority"""
    name: str
    coroutine: Callable
    timeout: float
    priority: int = 0  # Lower number = higher priority
    critical: bool = False  # If True, failure blocks shutdown

class DHTPublisherShutdownHandler:
    """Handles graceful shutdown of DHT publisher and related components"""
    
    def __init__(self, max_shutdown_time: float = 8.0):
        self.max_shutdown_time = max_shutdown_time
        self.shutdown_tasks: List[ShutdownTask] = []
        self.shutdown_phase = ShutdownPhase.INITIATED
        self.shutdown_start_time = 0
        self.shutdown_event = asyncio.Event()
        self.force_shutdown = False
        
        # Track components for shutdown
        self.dht_publisher = None
        self.dht_discovery = None
        self.heartbeat_manager = None
        self.p2p_handler = None
        self.sse_handler = None
        self.sse_network_monitor = None
        
        # Shutdown statistics
        self.shutdown_stats = {
            'tasks_completed': 0,
            'tasks_failed': 0,
            'tasks_timed_out': 0,
            'total_time': 0
        }
    
    def register_component(self, name: str, component: Any):
        """Register a component for shutdown"""
        setattr(self, name, component)
        logger.debug(f"Registered component for shutdown: {name}")
    
    def add_shutdown_task(self, name: str, coroutine: Callable, 
                         timeout: float = 2.0, priority: int = 0, critical: bool = False):
        """Add a shutdown task with specified priority and timeout"""
        task = ShutdownTask(
            name=name,
            coroutine=coroutine,
            timeout=timeout,
            priority=priority,
            critical=critical
        )
        self.shutdown_tasks.append(task)
        logger.debug(f"Added shutdown task: {name} (priority: {priority}, timeout: {timeout}s)")
    
    async def initiate_shutdown(self, reason: str = "graceful_shutdown"):
        """Initiate graceful shutdown sequence"""
        if self.shutdown_event.is_set():
            logger.warning("Shutdown already in progress")
            return
        
        self.shutdown_start_time = time.time()
        self.shutdown_event.set()
        self.shutdown_phase = ShutdownPhase.INITIATED
        
        logger.info(f"🛑 Initiating graceful shutdown: {reason}")
        logger.info(f"⏱️ Maximum shutdown time: {self.max_shutdown_time}s")
        
        try:
            # Phase 1: Send departure notifications
            await self._send_departure_notifications(reason)
            
            # Phase 2: Stop services in priority order
            self.shutdown_phase = ShutdownPhase.STOPPING_SERVICES
            await self._stop_services()
            
            # Phase 3: Clean up resources
            self.shutdown_phase = ShutdownPhase.CLEANING_UP
            await self._cleanup_resources()
            
            self.shutdown_phase = ShutdownPhase.COMPLETED
            total_time = time.time() - self.shutdown_start_time
            self.shutdown_stats['total_time'] = total_time
            
            logger.info(f"✅ Graceful shutdown completed in {total_time:.2f}s")
            self._log_shutdown_stats()
        
        except Exception as e:
            self.shutdown_phase = ShutdownPhase.FAILED
            total_time = time.time() - self.shutdown_start_time
            logger.error(f"❌ Shutdown failed after {total_time:.2f}s: {e}")
            raise
    
    async def _send_departure_notifications(self, reason: str):
        """Send departure notifications to the network"""
        logger.info("📤 Sending departure notifications...")
        
        departure_tasks = []
        
        # DHT departure notification
        if self.dht_publisher:
            try:
                departure_task = asyncio.create_task(
                    self.dht_publisher._broadcast_node_event("node_left", {
                        'node_id': getattr(self.dht_publisher.config, 'node_id', 'unknown'),
                        'reason': reason,
                        'timestamp': time.time(),
                        'graceful': True
                    })
                )
                departure_tasks.append(("dht_departure", departure_task))
            except Exception as e:
                logger.debug(f"Could not create DHT departure task: {e}")
        
        # SSE departure notification
        if self.sse_handler:
            try:
                departure_task = asyncio.create_task(
                    self.sse_handler.broadcast_event("node_shutdown", {
                        'reason': reason,
                        'timestamp': time.time(),
                        'graceful': True
                    })
                )
                departure_tasks.append(("sse_departure", departure_task))
            except Exception as e:
                logger.debug(f"Could not create SSE departure task: {e}")
        
        # Execute departure notifications with timeout
        for name, task in departure_tasks:
            try:
                await asyncio.wait_for(task, timeout=2.0)
                logger.debug(f"✅ {name} notification sent")
            except asyncio.TimeoutError:
                logger.warning(f"⏰ {name} notification timed out")
                task.cancel()
            except Exception as e:
                logger.debug(f"❌ {name} notification failed: {e}")
    
    async def _stop_services(self):
        """Stop all services in priority order"""
        logger.info("🔄 Stopping services...")
        
        # Build shutdown tasks in priority order
        self._build_shutdown_tasks()
        
        # Sort by priority (lower number = higher priority)
        self.shutdown_tasks.sort(key=lambda t: t.priority)
        
        # Execute shutdown tasks
        for task in self.shutdown_tasks:
            if self._is_shutdown_time_exceeded():
                logger.warning(f"⏰ Shutdown time exceeded, skipping {task.name}")
                break
            
            await self._execute_shutdown_task(task)
    
    def _build_shutdown_tasks(self):
        """Build the list of shutdown tasks based on registered components"""
        self.shutdown_tasks.clear()
        
        # Priority 0: Stop accepting new requests
        if self.sse_handler:
            self.add_shutdown_task(
                "sse_handler_stop_accepting",
                self._stop_sse_accepting,
                timeout=0.5,
                priority=0
            )
        
        # Priority 1: Cancel background monitoring tasks
        if self.dht_publisher and hasattr(self.dht_publisher, 'monitor_task'):
            self.add_shutdown_task(
                "dht_publisher_monitor_task",
                lambda: self._cancel_task_impl(self.dht_publisher.monitor_task, "DHT publisher monitor"),
                timeout=1.0,
                priority=1
            )
        
        if self.dht_discovery and hasattr(self.dht_discovery, 'event_processor_task'):
            self.add_shutdown_task(
                "dht_discovery_event_processor",
                lambda: self._cancel_task_impl(self.dht_discovery.event_processor_task, "DHT discovery event processor"),
                timeout=1.0,
                priority=1
            )
        
        if self.dht_discovery and hasattr(self.dht_discovery, 'dht_monitor_task'):
            self.add_shutdown_task(
                "dht_discovery_monitor",
                lambda: self._cancel_task_impl(self.dht_discovery.dht_monitor_task, "DHT discovery monitor"),
                timeout=1.0,
                priority=1
            )
        
        if self.dht_discovery and hasattr(self.dht_discovery, 'unknown_nodes_task'):
            self.add_shutdown_task(
                "dht_discovery_unknown_nodes",
                lambda: self._cancel_task_impl(self.dht_discovery.unknown_nodes_task, "DHT discovery unknown nodes"),
                timeout=1.0,
                priority=1
            )
        
        if self.sse_network_monitor and hasattr(self.sse_network_monitor, 'monitor_task'):
            self.add_shutdown_task(
                "sse_network_monitor_task",
                lambda: self._cancel_task_impl(self.sse_network_monitor.monitor_task, "SSE network monitor"),
                timeout=1.0,
                priority=1
            )
        
        if self.heartbeat_manager and hasattr(self.heartbeat_manager, 'heartbeat_task'):
            self.add_shutdown_task(
                "heartbeat_manager_task",
                lambda: self._cancel_task_impl(self.heartbeat_manager.heartbeat_task, "Heartbeat manager"),
                timeout=1.0,
                priority=1
            )
        
        # Priority 2: Stop service components
        if self.heartbeat_manager:
            self.add_shutdown_task(
                "heartbeat_manager_stop",
                lambda: self._safe_stop_impl(self.heartbeat_manager, "heartbeat manager"),
                timeout=1.0,
                priority=2
            )
        
        if self.sse_network_monitor:
            self.add_shutdown_task(
                "sse_network_monitor_stop",
                lambda: self._safe_stop_impl(self.sse_network_monitor, "SSE network monitor"),
                timeout=1.0,
                priority=2
            )
        
        if self.p2p_handler:
            self.add_shutdown_task(
                "p2p_handler_close",
                lambda: self._safe_close_impl(self.p2p_handler, "P2P handler"),
                timeout=1.0,
                priority=2
            )
        
        # Priority 3: Stop DHT components (most critical)
        if self.dht_publisher:
            self.add_shutdown_task(
                "dht_publisher_stop",
                self._stop_dht_publisher,
                timeout=2.0,
                priority=3,
                critical=True
            )
        
        if self.dht_discovery:
            self.add_shutdown_task(
                "dht_discovery_stop",
                self._stop_dht_discovery,
                timeout=2.0,
                priority=3,
                critical=True
            )
    
    async def _execute_shutdown_task(self, task: ShutdownTask):
        """Execute a single shutdown task with timeout and error handling"""
        start_time = time.time()
        
        try:
            logger.debug(f"🔄 Executing shutdown task: {task.name}")
            await asyncio.wait_for(task.coroutine(), timeout=task.timeout)
            
            execution_time = time.time() - start_time
            logger.debug(f"✅ {task.name} completed in {execution_time:.2f}s")
            self.shutdown_stats['tasks_completed'] += 1
            
        except asyncio.TimeoutError:
            execution_time = time.time() - start_time
            logger.warning(f"⏰ {task.name} timed out after {execution_time:.2f}s")
            self.shutdown_stats['tasks_timed_out'] += 1
            
            if task.critical:
                logger.error(f"❌ Critical task {task.name} timed out")
                raise
        
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"❌ {task.name} failed after {execution_time:.2f}s: {e}")
            self.shutdown_stats['tasks_failed'] += 1
            
            if task.critical:
                logger.error(f"❌ Critical task {task.name} failed")
                raise
    
    async def _cleanup_resources(self):
        """Clean up remaining resources"""
        logger.info("🧹 Cleaning up resources...")
        
        try:
            # Stop shared DHT service
            from common.dht_service import SharedDHTService
            dht_service = SharedDHTService()
            
            if dht_service.is_initialized():
                # Force immediate stop without waiting
                if dht_service._kademlia_node:
                    dht_service._kademlia_node.running = False
                
                # Don't wait for DHT cleanup - let it happen in background
                asyncio.create_task(dht_service.fast_stop())
                logger.debug("✅ DHT service cleanup initiated (background)")
            
        except Exception as e:
            logger.debug(f"DHT service cleanup error (non-blocking): {e}")
    
    def _is_shutdown_time_exceeded(self) -> bool:
        """Check if maximum shutdown time has been exceeded"""
        elapsed = time.time() - self.shutdown_start_time
        return elapsed > self.max_shutdown_time
    
    async def _stop_sse_accepting(self):
        """Stop SSE handler from accepting new connections"""
        if self.sse_handler:
            self.sse_handler.running = False
            logger.debug("SSE handler stopped accepting new connections")
    
    async def _cancel_task_impl(self, task: Optional[asyncio.Task], name: str):
        """Implementation for cancelling an asyncio task safely"""
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug(f"✅ {name} task cancelled")
            except Exception as e:
                logger.debug(f"❌ Error cancelling {name} task: {e}")
        else:
            logger.debug(f"✅ {name} task already done or None")

    async def _safe_stop_impl(self, component: Any, name: str):
        """Implementation for safely stopping a component with stop() method"""
        if component and hasattr(component, 'stop'):
            await component.stop()
            logger.debug(f"✅ {name} stopped")
        else:
            logger.debug(f"✅ {name} has no stop method or is None")

    async def _safe_close_impl(self, component: Any, name: str):
        """Implementation for safely closing a component with close() method"""
        if component and hasattr(component, 'close'):
            await component.close()
            logger.debug(f"✅ {name} closed")
        else:
            logger.debug(f"✅ {name} has no close method or is None")
    
    async def _stop_dht_publisher(self):
        """Stop DHT publisher with enhanced cleanup"""
        if not self.dht_publisher:
            return
        
        # Set running flag to False immediately
        self.dht_publisher.running = False
        
        # Quick unpublish attempt
        try:
            await asyncio.wait_for(self.dht_publisher._unpublish_node_info(), timeout=1.0)
            logger.debug("✅ DHT publisher node info unpublished")
        except asyncio.TimeoutError:
            logger.warning("⏰ DHT publisher unpublish timed out")
        except Exception as e:
            logger.debug(f"DHT publisher unpublish failed: {e}")
        
        # Clear references
        self.dht_publisher.kademlia_node = None
        logger.debug("✅ DHT publisher stopped")
    
    async def _stop_dht_discovery(self):
        """Stop DHT discovery with enhanced cleanup"""
        if not self.dht_discovery:
            return
        
        # Set running flag to False immediately
        self.dht_discovery.running = False
        
        # Clear state
        if hasattr(self.dht_discovery, 'active_nodes'):
            self.dht_discovery.active_nodes.clear()
        if hasattr(self.dht_discovery, 'known_node_ids'):
            self.dht_discovery.known_node_ids.clear()
        if hasattr(self.dht_discovery, 'event_listeners'):
            self.dht_discovery.event_listeners.clear()
        
        logger.debug("✅ DHT discovery stopped")
    
    def _log_shutdown_stats(self):
        """Log shutdown statistics"""
        stats = self.shutdown_stats
        total_tasks = stats['tasks_completed'] + stats['tasks_failed'] + stats['tasks_timed_out']
        
        logger.info(f"📊 Shutdown Statistics:")
        logger.info(f"   Total tasks: {total_tasks}")
        logger.info(f"   Completed: {stats['tasks_completed']}")
        logger.info(f"   Failed: {stats['tasks_failed']}")
        logger.info(f"   Timed out: {stats['tasks_timed_out']}")
        logger.info(f"   Total time: {stats['total_time']:.2f}s")
        
        if stats['tasks_failed'] > 0 or stats['tasks_timed_out'] > 0:
            logger.warning(f"⚠️ Shutdown completed with {stats['tasks_failed']} failures and {stats['tasks_timed_out']} timeouts")
    
    def force_shutdown_now(self):
        """Force immediate shutdown without waiting"""
        logger.warning("🚨 Force shutdown initiated")
        self.force_shutdown = True
        self.shutdown_event.set()
    
    def is_shutdown_in_progress(self) -> bool:
        """Check if shutdown is currently in progress"""
        return self.shutdown_event.is_set()
    
    def get_shutdown_status(self) -> Dict[str, Any]:
        """Get current shutdown status"""
        elapsed = time.time() - self.shutdown_start_time if self.shutdown_start_time else 0
        
        return {
            'phase': self.shutdown_phase.value,
            'elapsed_time': elapsed,
            'max_time': self.max_shutdown_time,
            'time_remaining': max(0, self.max_shutdown_time - elapsed),
            'force_shutdown': self.force_shutdown,
            'stats': self.shutdown_stats.copy()
        }


class SignalHandler:
    """Handle system signals for graceful shutdown"""
    
    def __init__(self, shutdown_handler: DHTPublisherShutdownHandler):
        self.shutdown_handler = shutdown_handler
        self.signals_registered = False
    
    def register_signals(self):
        """Register signal handlers for graceful shutdown"""
        if self.signals_registered:
            return
        
        try:
            # Register signal handlers
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
            
            self.signals_registered = True
            logger.info("✅ Signal handlers registered for graceful shutdown")
            
        except Exception as e:
            logger.warning(f"Could not register signal handlers: {e}")
    
    def _signal_handler(self, signum: int, frame):
        """Handle shutdown signals"""
        signal_name = signal.Signals(signum).name
        logger.info(f"🛑 Received {signal_name} signal, initiating graceful shutdown...")
        
        # Create shutdown task
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No running event loop found for signal handling")
            import os
            os._exit(1)
            return
        
        # Schedule shutdown
        if not self.shutdown_handler.is_shutdown_in_progress():
            shutdown_task = asyncio.create_task(
                self.shutdown_handler.initiate_shutdown(f"signal_{signal_name.lower()}")
            )
            
            # Ensure process exits after shutdown completes or times out
            def monitor_shutdown():
                import time
                import os
                
                # Wait for shutdown to complete (max 8 seconds to match shutdown handler)
                for i in range(80):  # 8 seconds in 0.1s intervals
                    if shutdown_task.done():
                        logger.info("✅ Graceful shutdown completed, exiting")
                        # Give uvicorn a moment to clean up
                        time.sleep(0.2)
                        os._exit(0)
                    time.sleep(0.1)
                
                # Force exit if shutdown takes too long
                logger.warning("⚠️ Shutdown timeout reached, forcing exit")
                os._exit(1)
            
            import threading
            threading.Thread(target=monitor_shutdown, daemon=True).start()
        else:
            logger.warning("Shutdown already in progress, forcing immediate exit")
            import os
            os._exit(0)
