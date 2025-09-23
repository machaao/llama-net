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
        """Get CPU information with fallback methods"""
        try:
            # Try multiple methods to get CPU info
            cpu_info = platform.processor()
            if cpu_info and cpu_info.strip():
                return cpu_info.strip()
            
            # Fallback to machine type
            machine = platform.machine()
            system = platform.system()
            return f"{system} {machine}"
        except Exception:
            return "Unknown CPU"
    
    @staticmethod
    def get_ram_info() -> Dict[str, Any]:
        """Get RAM information with better formatting"""
        try:
            mem = psutil.virtual_memory()
            return {
                "total": mem.total,
                "available": mem.available,
                "total_gb": round(mem.total / (1024**3), 2),
                "available_gb": round(mem.available / (1024**3), 2),
                "used_percent": mem.percent
            }
        except Exception as e:
            logger.error(f"Error getting RAM info: {e}")
            return {
                "total": 0,
                "available": 0,
                "total_gb": 0.0,
                "available_gb": 0.0,
                "used_percent": 0.0
            }
    
    @staticmethod
    def get_gpu_info() -> Optional[str]:
        """Get GPU information with better error handling"""
        try:
            gpus = GPUtil.getGPUs()
            if not gpus:
                return None
            
            # Return info for all GPUs
            gpu_info = []
            for gpu in gpus:
                memory_mb = int(gpu.memoryTotal)
                gpu_info.append(f"{gpu.name} ({memory_mb}MB)")
            
            return ", ".join(gpu_info)
        except ImportError:
            logger.debug("GPUtil not available")
            return None
        except Exception as e:
            logger.warning(f"Could not get GPU info: {e}")
            return None
    
    @staticmethod
    def get_current_load() -> Dict[str, float]:
        """Get current system load metrics without blocking"""
        try:
            # Use non-blocking CPU measurement
            cpu_percent = psutil.cpu_percent(interval=0)  # Non-blocking
            memory_percent = psutil.virtual_memory().percent
            
            # Safe disk usage check
            try:
                disk_percent = psutil.disk_usage('/').percent
            except (OSError, PermissionError):
                # Fallback for Windows or permission issues
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
            logger.error(f"Error getting load metrics: {e}")
            return {
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "disk_percent": 0.0
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
