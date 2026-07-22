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
    TIMED_OUT = "timed_out"

@dataclass
class QueuedRequest:
    request_id: str
    request_type: str
    request_data: Dict[str, Any]
    future: asyncio.Future
    queued_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: RequestStatus = RequestStatus.QUEUED
    owner_key: Optional[str] = None

class RequestQueueManager:
    """Manages request queuing for single-threaded LLM processing with fairness and timeouts"""

    MAX_QUEUE_SIZE = 50
    MAX_QUEUE_WAIT_SECONDS = 60
    MAX_PER_KEY_IN_QUEUE = 10
    PROCESSING_TIMEOUT_SECONDS = 120

    def __init__(self, max_queue_size: int = 50):
        self.max_queue_size = max_queue_size
        self.request_queue = asyncio.Queue(maxsize=max_queue_size)
        self.active_requests: Dict[str, QueuedRequest] = {}
        self.processing_request: Optional[QueuedRequest] = None
        self.worker_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self.running = False
        self.reloading = False
        self.stats = {
            "total_requests": 0,
            "completed_requests": 0,
            "failed_requests": 0,
            "cancelled_requests": 0,
            "timed_out_requests": 0,
            "queue_full_rejections": 0,
            "fairness_rejections": 0,
            "wait_timeout_rejections": 0,
        }

    async def start(self):
        """Start the request queue worker"""
        if self.running:
            return
        self.running = True
        self.worker_task = asyncio.create_task(self._worker_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Request queue manager started")

    async def stop(self):
        """Stop the request queue manager"""
        if not self.running:
            return
        self.running = False
        while not self.request_queue.empty():
            try:
                request = self.request_queue.get_nowait()
                request.status = RequestStatus.CANCELLED
                request.future.cancel()
                self.stats["cancelled_requests"] += 1
            except asyncio.QueueEmpty:
                break
        if self.processing_request:
            self.processing_request.status = RequestStatus.CANCELLED
            self.processing_request.future.cancel()
        for task in [self.worker_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Request queue manager stopped")

    async def set_reloading(self, is_reloading: bool):
        """Set the reloading state - pauses/resumes request processing"""
        self.reloading = is_reloading
        if is_reloading:
            logger.info("Request queue entering RELOADING state")
        else:
            logger.info("Request queue exiting RELOADING state")

    async def drain_active_requests(self, timeout: float = 30.0):
        """Wait for in-flight requests to complete"""
        if not self.processing_request:
            logger.info("No active requests to drain")
            return True
        logger.info(f"Draining active request: {self.processing_request.request_id}")
        start = time.time()
        while self.processing_request and (time.time() - start) < timeout:
            await asyncio.sleep(0.5)
        if self.processing_request:
            logger.warning(f"Drain timeout after {timeout}s")
            return False
        logger.info("All active requests drained successfully")
        return True

    def _get_owner_key(self, request_data: Dict[str, Any]) -> str:
        """Derive an owner key from request data for fairness enforcement"""
        headers = request_data.get("headers", {})
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:19]
        return request_data.get("client_ip", "anonymous")

    async def submit_request(self,
                           request_type: str,
                           request_data: Dict[str, Any],
                           processor: Callable[[Dict[str, Any]], Awaitable[Any]],
                           owner_key: Optional[str] = None) -> Any:
        """Submit a request to the queue with fairness enforcement"""
        if not self.running:
            raise RuntimeError("Request queue manager not running")

        effective_key = owner_key or self._get_owner_key(request_data)
        key_count = sum(
            1 for r in self.active_requests.values()
            if r.owner_key == effective_key and r.status == RequestStatus.QUEUED
        )
        if key_count >= self.MAX_PER_KEY_IN_QUEUE:
            self.stats["fairness_rejections"] += 1
            raise asyncio.QueueFull(
                f"Too many queued requests for this key. Maximum {self.MAX_PER_KEY_IN_QUEUE} allowed."
            )

        if self.request_queue.qsize() >= self.max_queue_size:
            self.stats["queue_full_rejections"] += 1
            raise asyncio.QueueFull("Request queue is full")

        request_id = f"{request_type}_{uuid.uuid4().hex[:8]}"
        future = asyncio.Future()

        queued_request = QueuedRequest(
            request_id=request_id,
            request_type=request_type,
            request_data=request_data,
            future=future,
            queued_at=time.time(),
            owner_key=effective_key,
        )
        queued_request.processor = processor

        await self.request_queue.put(queued_request)
        self.active_requests[request_id] = queued_request
        self.stats["total_requests"] += 1

        logger.debug(f"Request {request_id} queued (queue: {self.request_queue.qsize()}, owner: {effective_key[:8]}...)")

        try:
            result = await asyncio.wait_for(
                future,
                timeout=self.MAX_QUEUE_WAIT_SECONDS + self.PROCESSING_TIMEOUT_SECONDS
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Request {request_id} timed out")
            self.stats["timed_out_requests"] += 1
            queued_request.status = RequestStatus.TIMED_OUT
            raise TimeoutError("Request timed out. The model may be busy.")
        except asyncio.CancelledError:
            logger.debug(f"Request {request_id} cancelled")
            raise
        finally:
            self.active_requests.pop(request_id, None)

    async def _cleanup_loop(self):
        """Periodically clean up stale queued requests"""
        while self.running:
            try:
                await asyncio.sleep(10)
                now = time.time()
                stale = []
                for req_id, req in list(self.active_requests.items()):
                    if req.status == RequestStatus.QUEUED:
                        wait_time = now - req.queued_at
                        if wait_time > self.MAX_QUEUE_WAIT_SECONDS:
                            stale.append(req)
                for req in stale:
                    logger.warning(
                        f"Dropping stale request {req.request_id} "
                        f"(waited {now - req.queued_at:.0f}s > {self.MAX_QUEUE_WAIT_SECONDS}s limit)"
                    )
                    req.status = RequestStatus.TIMED_OUT
                    if not req.future.done():
                        req.future.set_exception(
                            TimeoutError(f"Request dropped: waited {self.MAX_QUEUE_WAIT_SECONDS}s in queue")
                        )
                    self.active_requests.pop(req.request_id, None)
                    self.stats["wait_timeout_rejections"] += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

    async def _worker_loop(self):
        """Main worker loop that processes requests sequentially"""
        logger.info("Request queue worker started")
        while self.running:
            try:
                if self.reloading:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    request = await asyncio.wait_for(
                        self.request_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                if request.status == RequestStatus.TIMED_OUT:
                    continue
                await self._process_request(request)
            except asyncio.CancelledError:
                logger.info("Request queue worker cancelled")
                break
            except Exception as e:
                logger.error(f"Error in request queue worker: {e}")
                await asyncio.sleep(0.1)
        logger.info("Request queue worker stopped")

    async def _process_request(self, request: QueuedRequest):
        """Process a single request with timeout protection"""
        self.processing_request = request
        request.status = RequestStatus.PROCESSING
        request.started_at = time.time()

        wait_time = request.started_at - request.queued_at
        logger.debug(f"Processing {request.request_id} (waited {wait_time:.2f}s)")

        try:
            result = await asyncio.wait_for(
                request.processor(request.request_data),
                timeout=self.PROCESSING_TIMEOUT_SECONDS
            )
            request.status = RequestStatus.COMPLETED
            request.completed_at = time.time()
            if not request.future.done():
                request.future.set_result(result)
            self.stats["completed_requests"] += 1
            processing_time = request.completed_at - request.started_at
            logger.debug(f"Request {request.request_id} completed in {processing_time:.2f}s")

        except asyncio.TimeoutError:
            request.status = RequestStatus.TIMED_OUT
            request.completed_at = time.time()
            if not request.future.done():
                request.future.set_exception(
                    TimeoutError(f"Processing timed out after {self.PROCESSING_TIMEOUT_SECONDS}s")
                )
            self.stats["timed_out_requests"] += 1
            logger.warning(f"Request {request.request_id} timed out during processing")

        except asyncio.CancelledError:
            request.status = RequestStatus.CANCELLED
            if not request.future.done():
                request.future.cancel()
            self.stats["cancelled_requests"] += 1

        except Exception as e:
            request.status = RequestStatus.FAILED
            request.completed_at = time.time()
            if not request.future.done():
                request.future.set_exception(e)
            self.stats["failed_requests"] += 1
            logger.error(f"Request {request.request_id} failed: {e}")

        finally:
            self.processing_request = None

    def get_status(self) -> Dict[str, Any]:
        """Get current queue status"""
        current_time = time.time()
        queue_wait_times = []
        for request in list(self.active_requests.values()):
            if request.status == RequestStatus.QUEUED:
                wait_time = current_time - request.queued_at
                queue_wait_times.append(wait_time)
        avg_wait_time = sum(queue_wait_times) / len(queue_wait_times) if queue_wait_times else 0
        return {
            "running": self.running,
            "queue_size": self.request_queue.qsize(),
            "max_queue_size": self.max_queue_size,
            "active_requests": len(self.active_requests),
            "processing_request": self.processing_request.request_id if self.processing_request else None,
            "avg_queue_wait_time": round(avg_wait_time, 2),
            "max_queue_wait_time": round(max(queue_wait_times), 2) if queue_wait_times else 0,
            "stats": self.stats.copy(),
            "timestamp": current_time
        }

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
        return self.request_queue.qsize()
