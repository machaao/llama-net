import asyncio
import logging
from typing import Any, Callable, Optional, Dict
from functools import wraps
from common.utils import get_logger

logger = get_logger(__name__)

class ErrorHandler:
    """Centralized error handling utilities"""
    
    @staticmethod
    def safe_async_call(func: Callable, error_message: str = "Operation failed", 
                       default_return: Any = None, log_level: int = logging.ERROR):
        """Decorator for safe async function calls with consistent error handling"""
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                logger.info(f"{func.__name__} was cancelled")
                raise
            except Exception as e:
                logger.log(log_level, f"{error_message}: {e}")
                return default_return
        return wrapper
    
    @staticmethod
    def safe_sync_call(func: Callable, error_message: str = "Operation failed", 
                      default_return: Any = None, log_level: int = logging.ERROR):
        """Decorator for safe sync function calls with consistent error handling"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.log(log_level, f"{error_message}: {e}")
                return default_return
        return wrapper
    
    @staticmethod
    async def safe_task_cancellation(task: Optional[asyncio.Task], task_name: str = "task"):
        """Safely cancel an asyncio task"""
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug(f"✅ {task_name} cancelled successfully")
            except Exception as e:
                logger.debug(f"❌ Error cancelling {task_name}: {e}")
        else:
            logger.debug(f"✅ {task_name} already done or None")
    
    @staticmethod
    async def safe_component_stop(component: Any, component_name: str = "component"):
        """Safely stop a component with stop() method"""
        if component and hasattr(component, 'stop'):
            try:
                await component.stop()
                logger.debug(f"✅ {component_name} stopped")
            except Exception as e:
                logger.debug(f"❌ Error stopping {component_name}: {e}")
        else:
            logger.debug(f"✅ {component_name} has no stop method or is None")
    
    @staticmethod
    async def safe_component_close(component: Any, component_name: str = "component"):
        """Safely close a component with close() method"""
        if component and hasattr(component, 'close'):
            try:
                await component.close()
                logger.debug(f"✅ {component_name} closed")
            except Exception as e:
                logger.debug(f"❌ Error closing {component_name}: {e}")
        else:
            logger.debug(f"✅ {component_name} has no close method or is None")
    
    @staticmethod
    def create_timeout_handler(timeout: float, operation_name: str = "operation"):
        """Create a timeout handler for operations"""
        async def timeout_handler():
            await asyncio.sleep(timeout)
            logger.warning(f"⏰ {operation_name} timed out after {timeout}s")
        return timeout_handler
    
    @staticmethod
    async def execute_with_timeout(coro, timeout: float, operation_name: str = "operation", 
                                  default_return: Any = None):
        """Execute a coroutine with timeout and error handling"""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"⏰ {operation_name} timed out after {timeout}s")
            return default_return
        except Exception as e:
            logger.error(f"❌ {operation_name} failed: {e}")
            return default_return
