import time
import psutil
from typing import Dict, Any, Optional
from common.utils import get_logger

logger = get_logger(__name__)

class MetricsManager:
    """Centralized metrics collection and management"""
    
    def __init__(self):
        self.start_time = time.time()
        self.total_tokens_generated = 0
        self.total_generation_time = 0
        self.request_count = 0
        self.active_requests = 0
        
        # Rolling-window samples for TTFT and latency (last 100 requests)
        self._ttft_samples: list = []
        self._latency_samples: list = []
        self._sample_window = 100
        
        # Callback for metrics updates (used by server to broadcast via SSE)
        self._on_metrics_updated = None
        self._event_loop = None  # Set by server for thread-safe scheduling
        
    def get_comprehensive_metrics(self) -> Dict[str, Any]:
        """Get all metrics in one call"""
        return {
            **self.get_performance_metrics(),
            **self.get_system_metrics(),
            **self.get_request_metrics()
        }
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance-related metrics"""
        uptime = int(time.time() - self.start_time)
        tps = self._calculate_tps()
        load = self._calculate_load()
        
        return {
            "uptime": uptime,
            "load": round(load, 2),
            "tps": round(tps, 2),
            "total_tokens": self.total_tokens_generated,
            "ttft": round(self.get_avg_ttft(), 3),
            "latency": round(self.get_avg_latency(), 3)
        }
    
    def get_system_metrics(self) -> Dict[str, Any]:
        """Get system-related metrics"""
        try:
            cpu_percent = psutil.cpu_percent(interval=0)
            memory_percent = psutil.virtual_memory().percent
            
            try:
                disk_percent = psutil.disk_usage('/').percent
            except (OSError, PermissionError):
                try:
                    disk_percent = psutil.disk_usage('.').percent
                except:
                    disk_percent = 0.0
            
            return {
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "disk_percent": disk_percent
            }
        except Exception as e:
            logger.error(f"Error getting system metrics: {e}")
            return {
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "disk_percent": 0.0
            }
    
    def get_request_metrics(self) -> Dict[str, Any]:
        """Get request-related metrics"""
        uptime = time.time() - self.start_time
        return {
            "active_requests": self.active_requests,
            "total_requests": self.request_count,
            "requests_per_second": self.request_count / uptime if uptime > 0 else 0
        }
    
    def _calculate_load(self) -> float:
        """Calculate current load"""
        return min(1.0, self.active_requests / 10)
    
    def _calculate_tps(self) -> float:
        """Calculate tokens per second"""
        if self.total_generation_time > 0:
            return self.total_tokens_generated / self.total_generation_time
        return 0.0
    
    def record_request_start(self):
        """Record request start"""
        self.active_requests += 1
        self.request_count += 1
    
    def record_request_end(self, tokens_generated: int = 0, generation_time: float = 0):
        """Record request completion"""
        self.active_requests = max(0, self.active_requests - 1)
        self.total_tokens_generated += tokens_generated
        self.total_generation_time += generation_time
        
        # Notify listeners that metrics have been updated
        # Must be thread-safe since record_request_end() is called from thread executors
        if self._on_metrics_updated:
            try:
                if self._event_loop and self._event_loop.is_running():
                    # Schedule callback on the main event loop from worker thread
                    self._event_loop.call_soon_threadsafe(self._on_metrics_updated)
                else:
                    # Already on main loop or loop not set
                    self._on_metrics_updated()
            except Exception as e:
                logger.debug(f"Metrics update callback error: {e}")
    
    def record_ttft(self, ttft_seconds: float):
        """Record a time-to-first-token sample"""
        self._ttft_samples.append(ttft_seconds)
        if len(self._ttft_samples) > self._sample_window:
            self._ttft_samples.pop(0)
    
    def record_latency(self, latency_seconds: float):
        """Record an end-to-end latency sample"""
        self._latency_samples.append(latency_seconds)
        if len(self._latency_samples) > self._sample_window:
            self._latency_samples.pop(0)
    
    def get_avg_ttft(self) -> float:
        """Get average time-to-first-token in seconds"""
        if not self._ttft_samples:
            return 0.0
        return sum(self._ttft_samples) / len(self._ttft_samples)
    
    def get_avg_latency(self) -> float:
        """Get average end-to-end latency in seconds"""
        if not self._latency_samples:
            return 0.0
        return sum(self._latency_samples) / len(self._latency_samples)
    
    def is_overloaded(self, cpu_threshold: float = 80.0, 
                     memory_threshold: float = 85.0,
                     max_requests: int = 10) -> Dict[str, Any]:
        """Check if node is overloaded"""
        system_metrics = self.get_system_metrics()
        
        overload_reasons = []
        
        if system_metrics["cpu_percent"] > cpu_threshold:
            overload_reasons.append(f"CPU: {system_metrics['cpu_percent']:.1f}% > {cpu_threshold}%")
        
        if system_metrics["memory_percent"] > memory_threshold:
            overload_reasons.append(f"Memory: {system_metrics['memory_percent']:.1f}% > {memory_threshold}%")
        
        if self.active_requests >= max_requests:
            overload_reasons.append(f"Requests: {self.active_requests} >= {max_requests}")
        
        return {
            "is_overloaded": len(overload_reasons) > 0,
            "overload_reasons": overload_reasons,
            "load_metrics": system_metrics,
            "active_requests": self.active_requests,
            "thresholds": {
                "cpu": cpu_threshold,
                "memory": memory_threshold, 
                "max_requests": max_requests
            }
        }
