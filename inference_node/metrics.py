import time
import psutil
import platform
from typing import Dict, Any, Optional
import GPUtil
from common.utils import get_logger

logger = get_logger(__name__)

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
    def get_all_info() -> Dict[str, Any]:
        """Get all system information"""
        return {
            "cpu": SystemInfo.get_cpu_info(),
            "ram": SystemInfo.get_ram_info(),
            "gpu": SystemInfo.get_gpu_info(),
            "platform": platform.platform()
        }
