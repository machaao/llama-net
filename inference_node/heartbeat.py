import asyncio
import time
from typing import Dict, Any, Callable
from common.utils import get_logger

logger = get_logger(__name__)

class HeartbeatManager:
    """Manages node health monitoring and heartbeat signals"""
    
    def __init__(self, node_id: str, metrics_callback: Callable[[], Dict[str, Any]], interval: int = 10, pool_status_callback: Callable = None):
        self.node_id = node_id
        self.metrics_callback = metrics_callback
        self.interval = interval
        self.running = False
        self.heartbeat_task = None
        self.last_heartbeat = 0
        self._last_heartbeat_wall_time = 0
        self._max_gap_detected = 0
        self._wake_events = 0
        self.pool_status_callback = pool_status_callback
        
    async def start(self):
        """Start the heartbeat system"""
        if self.running:
            return
            
        self.running = True
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Heartbeat manager started for node {self.node_id[:8]}...")
        
    async def stop(self):
        """Stop the heartbeat system"""
        self.running = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat manager stopped")
        
    async def _heartbeat_loop(self):
        """Main heartbeat loop"""
        while self.running:
            try:
                await self._send_heartbeat()
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying
                
    async def _send_heartbeat(self):
        """Send a heartbeat signal"""
        current_time = time.time()
        metrics = self.metrics_callback()

        # Wake-from-sleep detection
        if self._last_heartbeat_wall_time > 0:
            gap = current_time - self._last_heartbeat_wall_time
            if gap > self.interval * 3:
                self._wake_events += 1
                self._max_gap_detected = max(self._max_gap_detected, gap)
                logger.warning(
                    f"⏰ Heartbeat gap: {gap:.0f}s (expected {self.interval}s) — "
                    f"possible wake from sleep (event #{self._wake_events})"
                )

        # Update last heartbeat time
        self._last_heartbeat_wall_time = current_time
        self.last_heartbeat = current_time
        
        # Include pool info if callback is set
        if self.pool_status_callback:
            try:
                metrics['pool'] = self.pool_status_callback()
            except Exception:
                pass

        # Log heartbeat
        pool_info = f" pool={metrics.get('pool', {}).get('used', '?')}" if metrics.get('pool') else ""
        logger.debug(f"💓 Heartbeat from {self.node_id[:8]}... - Load: {metrics.get('load', 0):.2f}, TPS: {metrics.get('tps', 0):.2f}{pool_info}")
        
    def is_healthy(self) -> bool:
        """Check if the node is healthy based on recent heartbeats"""
        if self.last_heartbeat == 0:
            return False
        return time.time() - self.last_heartbeat < (self.interval * 3)  # Allow 3 missed heartbeats
        
    def get_health_status(self) -> Dict[str, Any]:
        """Get detailed health status"""
        current_time = time.time()
        time_since_last = current_time - self.last_heartbeat if self.last_heartbeat > 0 else float('inf')
        
        return {
            "healthy": self.is_healthy(),
            "running": self.running,
            "last_heartbeat": self.last_heartbeat,
            "time_since_last_heartbeat": time_since_last,
            "heartbeat_interval": self.interval,
            "wake_events": self._wake_events,
            "max_gap_detected": round(self._max_gap_detected, 1),
        }
