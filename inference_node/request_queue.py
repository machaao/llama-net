import asyncio
import time
import uuid
from typing import Dict, Any, Optional, Callable, Awaitable
from enum import Enum
from dataclasses import dataclass
from common.utils import get_logger

logger = get_logger(__name__)

class RequestStatus(Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class QueuedRequest:
    request_id: str
    request_type: str  # "completion" or "chat_completion"
    request_data: Dict[str, Any]
    future: asyncio.Future
    queued_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: RequestStatus = RequestStatus.QUEUED

class RequestQueueManager:
    """Manages request queuing for single-threaded LLM processing"""
    
    def __init__(self, max_queue_size: int = 50):
        self.max_queue_size = max_queue_size
        self.request_queue = asyncio.Queue(maxsize=max_queue_size)
        self.active_requests: Dict[str, QueuedRequest] = {}
        self.processing_request: Optional[QueuedRequest] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.running = False
        self.stats = {
            "total_requests": 0,
            "completed_requests": 0,
            "failed_requests": 0,
            "cancelled_requests": 0,
            "queue_full_rejections": 0
        }
        
    async def start(self):
        """Start the request queue worker"""
        if self.running:
            return
            
        self.running = True
        self.worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Request queue manager started")
        
    async def stop(self):
        """Stop the request queue manager"""
        if not self.running:
            return
            
        self.running = False
        
        # Cancel all queued requests
        while not self.request_queue.empty():
            try:
                request = self.request_queue.get_nowait()
                request.status = RequestStatus.CANCELLED
                request.future.cancel()
                self.stats["cancelled_requests"] += 1
            except asyncio.QueueEmpty:
                break
                
        # Cancel processing request if any
        if self.processing_request:
            self.processing_request.status = RequestStatus.CANCELLED
            self.processing_request.future.cancel()
            
        # Cancel worker task
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Request queue manager stopped")
        
    async def submit_request(self, 
                           request_type: str, 
                           request_data: Dict[str, Any],
                           processor: Callable[[Dict[str, Any]], Awaitable[Any]]) -> Any:
        """Submit a request to the queue"""
        if not self.running:
            raise RuntimeError("Request queue manager not running")
            
        # Check if queue is full
        if self.request_queue.qsize() >= self.max_queue_size:
            self.stats["queue_full_rejections"] += 1
            raise asyncio.QueueFull("Request queue is full")
            
        # Create request
        request_id = f"{request_type}_{uuid.uuid4().hex[:8]}"
        future = asyncio.Future()
        
        queued_request = QueuedRequest(
            request_id=request_id,
            request_type=request_type,
            request_data=request_data,
            future=future,
            queued_at=time.time()
        )
        
        # Store processor function in request data
        queued_request.processor = processor
        
        # Add to queue and tracking
        await self.request_queue.put(queued_request)
        self.active_requests[request_id] = queued_request
        self.stats["total_requests"] += 1
        
        logger.debug(f"Request {request_id} queued (queue size: {self.request_queue.qsize()})")
        
        try:
            # Wait for completion
            result = await future
            return result
        except asyncio.CancelledError:
            logger.debug(f"Request {request_id} cancelled")
            raise
        finally:
            # Clean up
            self.active_requests.pop(request_id, None)
            
    async def _worker_loop(self):
        """Main worker loop that processes requests sequentially"""
        logger.info("Request queue worker started")
        
        while self.running:
            try:
                # Get next request (with timeout to allow graceful shutdown)
                try:
                    request = await asyncio.wait_for(
                        self.request_queue.get(), 
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                    
                # Process the request
                await self._process_request(request)
                
            except asyncio.CancelledError:
                logger.info("Request queue worker cancelled")
                break
            except Exception as e:
                logger.error(f"Error in request queue worker: {e}")
                await asyncio.sleep(0.1)  # Brief pause on error
                
        logger.info("Request queue worker stopped")
        
    async def _process_request(self, request: QueuedRequest):
        """Process a single request"""
        self.processing_request = request
        request.status = RequestStatus.PROCESSING
        request.started_at = time.time()
        
        wait_time = request.started_at - request.queued_at
        logger.debug(f"Processing request {request.request_id} (waited {wait_time:.2f}s)")
        
        try:
            # Call the processor function
            result = await request.processor(request.request_data)
            
            # Mark as completed
            request.status = RequestStatus.COMPLETED
            request.completed_at = time.time()
            request.future.set_result(result)
            self.stats["completed_requests"] += 1
            
            processing_time = request.completed_at - request.started_at
            logger.debug(f"Request {request.request_id} completed in {processing_time:.2f}s")
            
        except asyncio.CancelledError:
            request.status = RequestStatus.CANCELLED
            request.future.cancel()
            self.stats["cancelled_requests"] += 1
            logger.debug(f"Request {request.request_id} cancelled during processing")
            
        except Exception as e:
            request.status = RequestStatus.FAILED
            request.completed_at = time.time()
            request.future.set_exception(e)
            self.stats["failed_requests"] += 1
            logger.error(f"Request {request.request_id} failed: {e}")
            
        finally:
            self.processing_request = None
            
    def get_status(self) -> Dict[str, Any]:
        """Get current queue status"""
        current_time = time.time()
        
        # Calculate queue wait times
        queue_wait_times = []
        for request in list(self.active_requests.values()):
            if request.status == RequestStatus.QUEUED:
                wait_time = current_time - request.queued_at
                queue_wait_times.append(wait_time)
                
        avg_wait_time = sum(queue_wait_times) / len(queue_wait_times) if queue_wait_times else 0
        
        status = {
            "running": self.running,
            "queue_size": self.request_queue.qsize(),
            "max_queue_size": self.max_queue_size,
            "active_requests": len(self.active_requests),
            "processing_request": self.processing_request.request_id if self.processing_request else None,
            "avg_queue_wait_time": avg_wait_time,
            "max_queue_wait_time": max(queue_wait_times) if queue_wait_times else 0,
            "stats": self.stats.copy(),
            "timestamp": current_time
        }
        
        return status
        
    def is_busy(self) -> bool:
        """Check if the LLM is currently processing a request"""
        return self.processing_request is not None
        
    def get_queue_position(self, request_id: str) -> Optional[int]:
        """Get the position of a request in the queue"""
        if request_id not in self.active_requests:
            return None
            
        request = self.active_requests[request_id]
        if request.status != RequestStatus.QUEUED:
            return None
            
        # This is approximate since asyncio.Queue doesn't provide position info
        return self.request_queue.qsize()
