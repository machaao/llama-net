import time
import psutil
import platform
from typing import Dict, Any, Optional
from common.utils import get_logger
import atexit
import threading

logger = get_logger(__name__)

# Global cleanup registry
_cleanup_registry = []
_cleanup_lock = threading.Lock()

def register_cleanup(cleanup_func):
    """Register a cleanup function to be called at exit"""
    with _cleanup_lock:
        _cleanup_registry.append(cleanup_func)

def _cleanup_all():
    """Clean up all registered resources"""
    with _cleanup_lock:
        for cleanup_func in _cleanup_registry:
            try:
                cleanup_func()
            except Exception as e:
                logger.debug(f"Cleanup error: {e}")
        _cleanup_registry.clear()

# Register cleanup at module import
atexit.register(_cleanup_all)

# RequestTracker functionality moved to common/metrics_manager.py
# Use MetricsManager for all request tracking

class SystemInfo:
    """Collect system information with proper resource management"""
    
    # Cache system info to avoid repeated psutil calls
    _cpu_info_cache = None
    _ram_info_cache = None
    _gpu_info_cache = None
    _cache_time = 0
    _cache_ttl = 300  # 5 minutes
    
    @staticmethod
    def get_cpu_info() -> str:
        """Get CPU information with caching and fallback methods"""
        current_time = time.time()
        
        # Use cache if available and fresh
        if (SystemInfo._cpu_info_cache and 
            current_time - SystemInfo._cache_time < SystemInfo._cache_ttl):
            return SystemInfo._cpu_info_cache
        
        try:
            # Try multiple methods to get CPU info
            cpu_info = platform.processor()
            if cpu_info and cpu_info.strip():
                SystemInfo._cpu_info_cache = cpu_info.strip()
                SystemInfo._cache_time = current_time
                return SystemInfo._cpu_info_cache
            
            # Fallback to machine type
            machine = platform.machine()
            system = platform.system()
            cpu_info = f"{system} {machine}"
            SystemInfo._cpu_info_cache = cpu_info
            SystemInfo._cache_time = current_time
            return cpu_info
        except Exception:
            fallback = "Unknown CPU"
            SystemInfo._cpu_info_cache = fallback
            return fallback
    
    @staticmethod
    def get_ram_info() -> Dict[str, Any]:
        """Get RAM information with better error handling and caching"""
        current_time = time.time()
        
        # Use cache if available and fresh
        if (SystemInfo._ram_info_cache and 
            current_time - SystemInfo._cache_time < SystemInfo._cache_ttl):
            return SystemInfo._ram_info_cache
        
        try:
            mem = psutil.virtual_memory()
            ram_info = {
                "total": mem.total,
                "available": mem.available,
                "total_gb": round(mem.total / (1024**3), 2),
                "available_gb": round(mem.available / (1024**3), 2),
                "used_percent": mem.percent
            }
            SystemInfo._ram_info_cache = ram_info
            SystemInfo._cache_time = current_time
            return ram_info
        except Exception as e:
            logger.error(f"Error getting RAM info: {e}")
            fallback = {
                "total": 0,
                "available": 0,
                "total_gb": 0.0,
                "available_gb": 0.0,
                "used_percent": 0.0
            }
            SystemInfo._ram_info_cache = fallback
            return fallback
    
    @staticmethod
    def get_gpu_info() -> Optional[str]:
        """Get GPU information with caching and improved error handling"""
        current_time = time.time()
        
        # Use cache if available and fresh
        if (SystemInfo._gpu_info_cache is not None and 
            current_time - SystemInfo._cache_time < SystemInfo._cache_ttl):
            return SystemInfo._gpu_info_cache
        
        try:
            # First check if we have NVIDIA GPUs using nvidia-smi (most reliable)
            import subprocess
            try:
                # Try nvidia-smi first (most reliable way to detect NVIDIA GPUs)
                result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    # Parse nvidia-smi output
                    gpu_info = []
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            parts = line.split(',')
                            if len(parts) >= 2:
                                name = parts[0].strip()
                                memory_mb = parts[1].strip()
                                gpu_info.append(f"{name} ({memory_mb}MB)")
                    
                    gpu_result = ", ".join(gpu_info) if gpu_info else None
                    SystemInfo._gpu_info_cache = gpu_result
                    SystemInfo._cache_time = current_time
                    return gpu_result
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                # nvidia-smi not available, continue to pynvml
                pass
            
            # Try pynvml as fallback
            import pynvml
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            
            if device_count == 0:
                SystemInfo._gpu_info_cache = None
                SystemInfo._cache_time = current_time
                return None
            
            # Return info for all GPUs
            gpu_info = []
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                memory_mb = int(memory_info.total / (1024 * 1024))
                
                # Handle both bytes and string returns from pynvml
                if isinstance(name, bytes):
                    name = name.decode('utf-8')
                
                gpu_info.append(f"{name} ({memory_mb}MB)")
            
            gpu_result = ", ".join(gpu_info)
            SystemInfo._gpu_info_cache = gpu_result
            SystemInfo._cache_time = current_time
            return gpu_result
            
        except ImportError:
            logger.debug("pynvml not available - install with: pip install pynvml")
            SystemInfo._gpu_info_cache = None
            SystemInfo._cache_time = current_time
            return None
        except Exception as e:
            # Only log as debug for common "no NVIDIA GPU" scenarios
            error_msg = str(e).lower()
            if any(phrase in error_msg for phrase in ['nvml shared library not found', 'nvidia driver', 'no devices', 'nvml_error_uninitialized']):
                logger.debug(f"No NVIDIA GPUs detected: {e}")
            else:
                logger.warning(f"Could not get GPU info: {e}")
            SystemInfo._gpu_info_cache = None
            SystemInfo._cache_time = current_time
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

    # Overload checking moved to common/metrics_manager.py
    # Use MetricsManager.is_overloaded() for consistent overload detection

    @staticmethod
    def get_all_info() -> Dict[str, Any]:
        """Get all system information with caching"""
        return {
            "cpu": SystemInfo.get_cpu_info(),
            "ram": SystemInfo.get_ram_info(),
            "gpu": SystemInfo.get_gpu_info(),
            "platform": platform.platform()
        }

# Module cleanup
def cleanup_system_info():
    """Clean up SystemInfo caches"""
    SystemInfo._cpu_info_cache = None
    SystemInfo._ram_info_cache = None
    SystemInfo._gpu_info_cache = None

register_cleanup(cleanup_system_info)
