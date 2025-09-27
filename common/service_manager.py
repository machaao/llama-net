import asyncio
import time
from typing import Dict, Set, Callable, Optional
from enum import Enum
from dataclasses import dataclass
from common.utils import get_logger

logger = get_logger(__name__)

class ServiceState(Enum):
    PENDING = "pending"
    INITIALIZING = "initializing" 
    READY = "ready"
    FAILED = "failed"

@dataclass
class ServiceInfo:
    name: str
    state: ServiceState
    start_time: Optional[float] = None
    ready_time: Optional[float] = None
    error: Optional[str] = None
    dependencies: Set[str] = None

    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = set()

class ServiceInitializationManager:
    """Manages service initialization order and tracks readiness for DHT join"""
    
    def __init__(self):
        self.services: Dict[str, ServiceInfo] = {}
        self.ready_callbacks: Dict[str, Callable] = {}
        self.all_ready_callbacks: list = []
        self.initialization_complete = False
        self._lock = asyncio.Lock()
        
        # Define service dependencies and initialization order
        self._define_service_dependencies()
    
    def _define_service_dependencies(self):
        """Define the required services and their dependencies"""
        # Core services that must be ready before DHT join
        self.register_service("config", dependencies=set())
        self.register_service("llm", dependencies={"config"})
        self.register_service("system_info", dependencies={"config"})
        self.register_service("heartbeat_manager", dependencies={"llm"})
        self.register_service("dht_service", dependencies={"config"})
        self.register_service("dht_publisher", dependencies={"dht_service"})
        self.register_service("dht_discovery", dependencies={"dht_service"})
        self.register_service("sse_handler", dependencies={"dht_discovery"})
        self.register_service("sse_network_monitor", dependencies={"sse_handler"})
        self.register_service("discovery_bridge", dependencies={"sse_handler", "dht_discovery"})
        self.register_service("node_selector", dependencies={"dht_discovery"})
        self.register_service("p2p_handler", dependencies={"llm"}, optional=True)
    
    def register_service(self, name: str, dependencies: Set[str] = None, optional: bool = False):
        """Register a service with its dependencies"""
        self.services[name] = ServiceInfo(
            name=name,
            state=ServiceState.PENDING,
            dependencies=dependencies or set()
        )
        if optional:
            self.services[name].optional = True
        logger.debug(f"Registered service: {name} with dependencies: {dependencies}")
    
    async def mark_service_initializing(self, name: str):
        """Mark a service as starting initialization"""
        async with self._lock:
            if name in self.services:
                self.services[name].state = ServiceState.INITIALIZING
                self.services[name].start_time = time.time()
                logger.debug(f"Service {name} is initializing")
    
    async def mark_service_ready(self, name: str):
        """Mark a service as ready and check if all services are ready"""
        async with self._lock:
            if name in self.services:
                self.services[name].state = ServiceState.READY
                self.services[name].ready_time = time.time()
                
                init_time = 0
                if self.services[name].start_time:
                    init_time = self.services[name].ready_time - self.services[name].start_time
                
                logger.info(f"✅ Service {name} ready (init time: {init_time:.2f}s)")
                
                # Execute ready callback if registered
                if name in self.ready_callbacks:
                    try:
                        await self.ready_callbacks[name]()
                    except Exception as e:
                        logger.error(f"Error in ready callback for {name}: {e}")
                
                # Check if all required services are ready
                await self._check_all_services_ready()
    
    async def mark_service_failed(self, name: str, error: str):
        """Mark a service as failed"""
        async with self._lock:
            if name in self.services:
                self.services[name].state = ServiceState.FAILED
                self.services[name].error = error
                logger.error(f"❌ Service {name} failed: {error}")
    
    def register_ready_callback(self, service_name: str, callback: Callable):
        """Register a callback to execute when a specific service is ready"""
        self.ready_callbacks[service_name] = callback
    
    def register_all_ready_callback(self, callback: Callable):
        """Register a callback to execute when all services are ready"""
        self.all_ready_callbacks.append(callback)
    
    async def _check_all_services_ready(self):
        """Check if all required services are ready and trigger callbacks"""
        if self.initialization_complete:
            return
        
        required_services = {name: service for name, service in self.services.items() 
                           if not getattr(service, 'optional', False)}
        
        all_ready = all(service.state == ServiceState.READY for service in required_services.values())
        
        if all_ready:
            self.initialization_complete = True
            total_time = time.time() - min(s.start_time for s in required_services.values() if s.start_time)
            
            logger.info(f"🎉 All services ready! Total initialization time: {total_time:.2f}s")
            
            # Execute all ready callbacks
            for callback in self.all_ready_callbacks:
                try:
                    await callback()
                except Exception as e:
                    logger.error(f"Error in all-ready callback: {e}")
    
    def are_dependencies_ready(self, service_name: str) -> bool:
        """Check if all dependencies for a service are ready"""
        if service_name not in self.services:
            return False
        
        service = self.services[service_name]
        for dep in service.dependencies:
            if dep not in self.services or self.services[dep].state != ServiceState.READY:
                return False
        return True
    
    def get_initialization_status(self) -> Dict:
        """Get current initialization status"""
        status = {
            "initialization_complete": self.initialization_complete,
            "services": {},
            "ready_count": 0,
            "total_count": len([s for s in self.services.values() if not getattr(s, 'optional', False)])
        }
        
        for name, service in self.services.items():
            status["services"][name] = {
                "state": service.state.value,
                "dependencies": list(service.dependencies),
                "optional": getattr(service, 'optional', False),
                "start_time": service.start_time,
                "ready_time": service.ready_time,
                "error": service.error
            }
            
            if service.state == ServiceState.READY and not getattr(service, 'optional', False):
                status["ready_count"] += 1
        
        return status
    
    async def wait_for_all_services(self, timeout: float = 30.0) -> bool:
        """Wait for all services to be ready with timeout"""
        start_time = time.time()
        
        while not self.initialization_complete and (time.time() - start_time) < timeout:
            await asyncio.sleep(0.1)
        
        return self.initialization_complete

# Global service manager instance
_service_manager: Optional[ServiceInitializationManager] = None

def get_service_manager() -> ServiceInitializationManager:
    """Get the global service manager instance"""
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceInitializationManager()
    return _service_manager
