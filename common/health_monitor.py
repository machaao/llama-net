import asyncio
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from common.models import NodeInfo
from common.network_utils import NetworkUtils
from common.utils import get_logger

logger = get_logger(__name__)

@dataclass
class HealthCheckResult:
    """Result of a health check for a node"""
    node_id: str
    ip: str
    port: int
    timestamp: float
    http_reachable: bool
    tcp_reachable: bool
    response_time_ms: float
    error_message: Optional[str] = None
    
    @property
    def is_healthy(self) -> bool:
        """Check if the node is considered healthy"""
        return self.http_reachable and self.tcp_reachable
    
    @property
    def health_score(self) -> float:
        """Calculate a health score (0-100)"""
        if not self.is_healthy:
            return 0.0
        
        # Base score for being reachable
        score = 70.0
        
        # Bonus for good response time
        if self.response_time_ms < 100:
            score += 30.0
        elif self.response_time_ms < 500:
            score += 20.0
        elif self.response_time_ms < 1000:
            score += 10.0
        
        return min(100.0, score)

class NodeHealthMonitor:
    """Monitors the health of discovered nodes"""
    
    def __init__(self, 
                 check_interval: float = 30.0,
                 timeout: float = 5.0,
                 max_failures: int = 3,
                 cleanup_interval: float = 300.0):
        self.check_interval = check_interval
        self.timeout = timeout
        self.max_failures = max_failures
        self.cleanup_interval = cleanup_interval
        
        # Health tracking
        self.health_history: Dict[str, List[HealthCheckResult]] = {}
        self.failure_counts: Dict[str, int] = {}
        self.last_cleanup = time.time()
        
        # Monitoring state
        self.monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self.on_node_unhealthy: Optional[Callable[[str, HealthCheckResult], None]] = None
        self.on_node_recovered: Optional[Callable[[str, HealthCheckResult], None]] = None
        
    async def start_monitoring(self, nodes: List[NodeInfo]):
        """Start monitoring the health of nodes"""
        if self.monitoring:
            logger.warning("Health monitoring already running")
            return
        
        self.monitoring = True
        logger.info(f"Starting health monitoring for {len(nodes)} nodes")
        
        # Initialize tracking for nodes
        for node in nodes:
            if node.node_id not in self.health_history:
                self.health_history[node.node_id] = []
                self.failure_counts[node.node_id] = 0
        
        # Start monitoring task
        self.monitor_task = asyncio.create_task(self._monitoring_loop(nodes))
    
    async def stop_monitoring(self):
        """Stop health monitoring"""
        self.monitoring = False
        
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Health monitoring stopped")
    
    async def _monitoring_loop(self, initial_nodes: List[NodeInfo]):
        """Main monitoring loop"""
        current_nodes = {node.node_id: node for node in initial_nodes}
        
        while self.monitoring:
            try:
                # Perform health checks
                await self._check_all_nodes(list(current_nodes.values()))
                
                # Cleanup old data periodically
                if time.time() - self.last_cleanup > self.cleanup_interval:
                    self._cleanup_old_data()
                    self.last_cleanup = time.time()
                
                # Wait for next check
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health monitoring loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying
    
    async def _check_all_nodes(self, nodes: List[NodeInfo]):
        """Check health of all nodes concurrently"""
        if not nodes:
            return
        
        # Create health check tasks
        tasks = [self._check_node_health(node) for node in nodes]
        
        # Execute all checks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Health check failed for node {nodes[i].node_id}: {result}")
            elif isinstance(result, HealthCheckResult):
                await self._process_health_result(result)
    
    async def _check_node_health(self, node: NodeInfo) -> HealthCheckResult:
        """Check the health of a single node"""
        start_time = time.time()
        
        try:
            # Test HTTP connectivity
            http_success = await NetworkUtils.test_http_connectivity(
                node.ip, node.port, self.timeout
            )
            
            # Test TCP connectivity
            tcp_success = await NetworkUtils.test_tcp_connectivity(
                node.ip, node.port, self.timeout
            )
            
            response_time = (time.time() - start_time) * 1000  # Convert to ms
            
            return HealthCheckResult(
                node_id=node.node_id,
                ip=node.ip,
                port=node.port,
                timestamp=time.time(),
                http_reachable=http_success,
                tcp_reachable=tcp_success,
                response_time_ms=response_time
            )
            
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            
            return HealthCheckResult(
                node_id=node.node_id,
                ip=node.ip,
                port=node.port,
                timestamp=time.time(),
                http_reachable=False,
                tcp_reachable=False,
                response_time_ms=response_time,
                error_message=str(e)
            )
    
    async def _process_health_result(self, result: HealthCheckResult):
        """Process a health check result"""
        node_id = result.node_id
        
        # Add to history
        if node_id not in self.health_history:
            self.health_history[node_id] = []
        
        self.health_history[node_id].append(result)
        
        # Limit history size (keep last 100 results)
        if len(self.health_history[node_id]) > 100:
            self.health_history[node_id] = self.health_history[node_id][-100:]
        
        # Update failure count
        if not result.is_healthy:
            self.failure_counts[node_id] = self.failure_counts.get(node_id, 0) + 1
            
            # Check if node should be marked as unhealthy
            if (self.failure_counts[node_id] >= self.max_failures and 
                self.on_node_unhealthy):
                self.on_node_unhealthy(node_id, result)
                
        else:
            # Node is healthy, check if it recovered
            previous_failures = self.failure_counts.get(node_id, 0)
            self.failure_counts[node_id] = 0
            
            if previous_failures >= self.max_failures and self.on_node_recovered:
                self.on_node_recovered(node_id, result)
        
        logger.debug(f"Health check for {node_id[:12]}...: "
                    f"healthy={result.is_healthy}, "
                    f"response_time={result.response_time_ms:.1f}ms")
    
    def _cleanup_old_data(self):
        """Clean up old health check data"""
        cutoff_time = time.time() - (24 * 3600)  # Keep 24 hours of data
        
        for node_id in list(self.health_history.keys()):
            # Remove old results
            self.health_history[node_id] = [
                result for result in self.health_history[node_id]
                if result.timestamp > cutoff_time
            ]
            
            # Remove empty entries
            if not self.health_history[node_id]:
                del self.health_history[node_id]
                if node_id in self.failure_counts:
                    del self.failure_counts[node_id]
        
        logger.debug(f"Cleaned up health data, tracking {len(self.health_history)} nodes")
    
    def get_node_health_status(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get health status for a specific node"""
        if node_id not in self.health_history:
            return None
        
        history = self.health_history[node_id]
        if not history:
            return None
        
        latest = history[-1]
        recent_results = [r for r in history if time.time() - r.timestamp < 300]  # Last 5 minutes
        
        if recent_results:
            avg_response_time = sum(r.response_time_ms for r in recent_results) / len(recent_results)
            success_rate = sum(1 for r in recent_results if r.is_healthy) / len(recent_results)
        else:
            avg_response_time = latest.response_time_ms
            success_rate = 1.0 if latest.is_healthy else 0.0
        
        return {
            "node_id": node_id,
            "is_healthy": latest.is_healthy,
            "health_score": latest.health_score,
            "last_check": latest.timestamp,
            "failure_count": self.failure_counts.get(node_id, 0),
            "avg_response_time_ms": round(avg_response_time, 2),
            "success_rate": round(success_rate, 3),
            "total_checks": len(history),
            "recent_checks": len(recent_results)
        }
    
    def get_all_health_status(self) -> Dict[str, Dict[str, Any]]:
        """Get health status for all monitored nodes"""
        status = {}
        for node_id in self.health_history:
            node_status = self.get_node_health_status(node_id)
            if node_status:
                status[node_id] = node_status
        return status
    
    def get_unhealthy_nodes(self) -> List[str]:
        """Get list of currently unhealthy node IDs"""
        unhealthy = []
        for node_id in self.health_history:
            if self.failure_counts.get(node_id, 0) >= self.max_failures:
                unhealthy.append(node_id)
        return unhealthy
    
    def get_health_summary(self) -> Dict[str, Any]:
        """Get overall health summary"""
        total_nodes = len(self.health_history)
        if total_nodes == 0:
            return {"total_nodes": 0, "healthy_nodes": 0, "unhealthy_nodes": 0}
        
        unhealthy_nodes = len(self.get_unhealthy_nodes())
        healthy_nodes = total_nodes - unhealthy_nodes
        
        # Calculate average health score
        all_status = self.get_all_health_status()
        if all_status:
            avg_health_score = sum(status["health_score"] for status in all_status.values()) / len(all_status)
            avg_response_time = sum(status["avg_response_time_ms"] for status in all_status.values()) / len(all_status)
        else:
            avg_health_score = 0.0
            avg_response_time = 0.0
        
        return {
            "total_nodes": total_nodes,
            "healthy_nodes": healthy_nodes,
            "unhealthy_nodes": unhealthy_nodes,
            "health_percentage": round((healthy_nodes / total_nodes) * 100, 1),
            "avg_health_score": round(avg_health_score, 1),
            "avg_response_time_ms": round(avg_response_time, 2),
            "last_update": time.time()
        }

class HealthAwareNodeFilter:
    """Filter nodes based on health status"""
    
    def __init__(self, health_monitor: NodeHealthMonitor):
        self.health_monitor = health_monitor
    
    def filter_healthy_nodes(self, nodes: List[NodeInfo], 
                           min_health_score: float = 50.0,
                           max_failure_count: int = 2) -> List[NodeInfo]:
        """Filter nodes to only include healthy ones"""
        healthy_nodes = []
        
        for node in nodes:
            health_status = self.health_monitor.get_node_health_status(node.node_id)
            
            if health_status is None:
                # No health data available, include by default
                healthy_nodes.append(node)
                continue
            
            # Check health criteria
            if (health_status["health_score"] >= min_health_score and
                health_status["failure_count"] <= max_failure_count):
                healthy_nodes.append(node)
            else:
                logger.debug(f"Filtered out unhealthy node {node.node_id[:12]}... "
                           f"(score: {health_status['health_score']}, "
                           f"failures: {health_status['failure_count']})")
        
        return healthy_nodes
    
    def sort_by_health(self, nodes: List[NodeInfo]) -> List[NodeInfo]:
        """Sort nodes by health score (best first)"""
        def get_health_score(node: NodeInfo) -> float:
            health_status = self.health_monitor.get_node_health_status(node.node_id)
            return health_status["health_score"] if health_status else 50.0
        
        return sorted(nodes, key=get_health_score, reverse=True)
