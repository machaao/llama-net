import asyncio
import time
from typing import Dict, Any, List, Optional
from common.utils import get_logger

logger = get_logger(__name__)


class GatewayEventPublisher:
    """Lightweight event publisher that delegates to GatewayClient for all communication."""

    def __init__(self, gateway_client, metrics_callback):
        self.gateway_client = gateway_client
        self.metrics_callback = metrics_callback
        self.running = False

        # Metrics-change tracking (event-driven, no periodic updates)
        self.last_published_metrics: Dict[str, Any] = {}
        self.metrics_change_threshold = 0.15
        self.last_significant_change: float = 0

        # Monitoring task
        self.monitor_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the event publisher."""
        if self.running:
            return

        self.running = True
        self.monitor_task = asyncio.create_task(self._monitor_changes())
        logger.info("Gateway event publisher started")

    async def stop(self):
        """Stop the publisher and send departure event."""
        logger.info("Stopping gateway event publisher...")
        self.running = False

        # Cancel monitoring
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        # Send departure event via gateway
        if self.gateway_client and self.gateway_client.registered:
            try:
                await asyncio.wait_for(
                    self.gateway_client.send_event("node_left"),
                    timeout=3.0
                )
                logger.info("✅ Departure event sent to gateway")
            except Exception as e:
                logger.debug(f"Departure event failed: {e}")

        logger.info("Gateway event publisher stopped")

    async def send_post_uvicorn_join_event(self):
        """Send join event after uvicorn is fully ready."""
        if not self.gateway_client:
            logger.warning("Cannot send join event - no gateway client")
            return

        try:
            await self.gateway_client.send_event("node_joined")
            logger.info("✅ Join event sent to gateway (post-uvicorn)")
        except Exception as e:
            logger.error(f"Failed to send join event: {e}")

    async def _monitor_changes(self):
        """Monitor metrics for significant changes and publish updates."""
        while self.running:
            try:
                await asyncio.sleep(30)

                if not self.gateway_client:
                    continue

                current_metrics = self.metrics_callback() if self.metrics_callback else {}

                if self._should_update_metrics(current_metrics):
                    await self.gateway_client.send_event("node_updated")
                    self.last_published_metrics = current_metrics.copy()
                    self.last_significant_change = time.time()
                    logger.debug("Published update due to significant metric change")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics monitor: {e}")
                await asyncio.sleep(60)

    def _should_update_metrics(self, current_metrics: Dict[str, Any]) -> bool:
        """Check if metrics changed enough to warrant an update."""
        if not self.last_published_metrics:
            return True

        for key in ['load', 'tps', 'ttft', 'latency']:
            if key in current_metrics and key in self.last_published_metrics:
                old_value = self.last_published_metrics[key]
                new_value = current_metrics[key]

                if old_value == 0 and new_value == 0:
                    continue

                if old_value == 0:
                    return True

                change_ratio = abs(new_value - old_value) / old_value
                if change_ratio > self.metrics_change_threshold:
                    return True

        return False
