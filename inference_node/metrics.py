import time
import psutil
import platform
from typing import Dict, Any, Optional
import GPUtil
from common.utils import get_logger

logger = get_logger(__name__)

class RequestTracker:
    """Track active requests for overload detection"""
    
    def __init__(self):
        self.active_requests = 0
        self.total_requests = 0
        self.start_time = time.time()
    
    def request_started(self):
        """Mark request as started"""
        self.active_requests += 1
        self.total_requests += 1
    
    def request_completed(self):
        """Mark request as completed"""
        self.active_requests = max(0, self.active_requests - 1)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get request statistics"""
        uptime = time.time() - self.start_time
        return {
            "active_requests": self.active_requests,
            "total_requests": self.total_requests,
            "requests_per_second": self.total_requests / uptime if uptime > 0 else 0,
            "uptime": uptime
        }

class SystemInfo:
    """Collect system information"""
    
    @staticmethod
    def get_cpu_info() -> str:
        """Get CPU information"""
        return platform.processor() or "Unknown CPU"
    
    @staticmethod
    def get_ram_info() -> Dict[str, int]:
        """Get RAM information"""
        mem = psutil.virtual_memory()
        return {
            "total": mem.total,
            "available": mem.available
        }
    
    @staticmethod
    def get_gpu_info() -> Optional[str]:
        """Get GPU information if available"""
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                return f"{gpus[0].name}, {gpus[0].memoryTotal}MB"
            return None
        except Exception as e:
            logger.warning(f"Could not get GPU info: {e}")
            return None
    
    @staticmethod
    def get_current_load() -> Dict[str, float]:
        """Get current system load metrics"""
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage('/').percent
        }

    @staticmethod
    def is_overloaded(cpu_threshold: float = 80.0, 
                     memory_threshold: float = 85.0,
                     active_requests: int = 0,
                     max_requests: int = 10) -> Dict[str, Any]:
        """Check if node is overloaded based on configurable thresholds"""
        load_metrics = SystemInfo.get_current_load()
        
        overload_reasons = []
        
        # Check CPU threshold
        if load_metrics["cpu_percent"] > cpu_threshold:
            overload_reasons.append(f"CPU: {load_metrics['cpu_percent']:.1f}% > {cpu_threshold}%")
        
        # Check memory threshold  
        if load_metrics["memory_percent"] > memory_threshold:
            overload_reasons.append(f"Memory: {load_metrics['memory_percent']:.1f}% > {memory_threshold}%")
        
        # Check request queue threshold
        if active_requests >= max_requests:
            overload_reasons.append(f"Requests: {active_requests} >= {max_requests}")
        
        return {
            "is_overloaded": len(overload_reasons) > 0,
            "overload_reasons": overload_reasons,
            "load_metrics": load_metrics,
            "active_requests": active_requests,
            "thresholds": {
                "cpu": cpu_threshold,
                "memory": memory_threshold, 
                "max_requests": max_requests
            }
        }

    @staticmethod
    def get_all_info() -> Dict[str, Any]:
        """Get all system information"""
        return {
            "cpu": SystemInfo.get_cpu_info(),
            "ram": SystemInfo.get_ram_info(),
            "gpu": SystemInfo.get_gpu_info(),
            "platform": platform.platform()
        }
