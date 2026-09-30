"""FastAPI router exposing the Fara1.5-9B computer-use agent API.

Registered on the inference node in ``inference_node/server.py``. Sessions
run in background asyncio tasks; per-step events are pushed to an in-memory
queue and streamed to the browser via SSE.

The router is created via :func:`create_agent_router` with a ``get_llm``
callable so it always resolves the *currently active* model (hot-swap/pool
aware) rather than holding a stale reference.
"""

import asyncio
import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from common.utils import get_logger
from agent.loop import AgentLoop, AgentState

logger = get_logger(__name__)


class AgentSession:
    """A running (or finished) agent session with an SSE event queue."""

    def __init__(self, loop: AgentLoop):
        self.loop = loop
        self.id = uuid.uuid4().hex[:12]
        self.created_at = time.time()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.task: Optional[asyncio.Task] = None

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            self.queue.put_nowait({"type": event_type, "timestamp": time.time(), **data})
        except asyncio.QueueFull:
            # Drop oldest to avoid unbounded growth under slow consumers.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait({"type": event_type, "timestamp": time.time(), **data})
            except Exception:
                pass


def create_agent_router(get_llm: Callable[[], Any]) -> APIRouter:
    """Build the agent APIRouter bound to an active-model resolver.

    Args:
        get_llm: Zero-arg callable returning the current ``LlamaWrapper``
            (or None when no model is loaded).
    """
    router = APIRouter(prefix="/agent", tags=["agent"])
    sessions: Dict[str, AgentSession] = {}

    def _start_loop_task(session: AgentSession) -> None:
        """Launch the loop as a background task with a done-callback that
        guarantees any unhandled exception is surfaced to the SSE stream.

        Without this callback, a crash inside run()/step() (e.g. a
        generate_chat_multimodal AttributeError, a browser crash, or a
        cancellation that bypasses the loop's own handlers) would leave the
        task dead with no event — the UI would show "running" forever with
        nothing happening.
        """
        async def _done(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is None:
                return
            # The loop failed outside its own try/except — surface it.
            logger.error(f"Agent session {session.id} crashed: {exc}", exc_info=True)
            from agent.loop import AgentState
            session.loop.state = AgentState.FAILED
            await session.emit("execution_error", error=str(exc))

        session.task = asyncio.create_task(session.loop.run())
        # Wire the task into the loop so stop() can cancel an in-flight step
        # immediately (loop._task is otherwise never set and cancel no-ops).
        session.loop._task = session.task
        session.task.add_done_callback(
            lambda t: asyncio.create_task(_done(t))
        )

    def _new_loop(goal: str, body: Dict[str, Any]) -> AgentLoop:
        loop = AgentLoop(
            llm=get_llm(),
            goal=goal,
            allowlist=body.get("allowlist") or [],
            blocklist=body.get("blocklist") or [],
            start_url=body.get("start_url") or None,
            headless=body.get("headless", True),
            max_steps=int(body.get("max_steps", AgentLoop.DEFAULT_MAX_STEPS)),
        )
        return loop

    @router.post("/sessions")
    async def create_session(body: Dict[str, Any]):
        goal = (body.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal is required")
        llm = get_llm()
        if llm is None:
            raise HTTPException(status_code=503, detail="No model loaded — load a vision model first")
        if not getattr(llm, "supports_vision", False):
            logger.warning(
                f"Active model '{getattr(llm, '_model_name', '?')}' does not appear to be a "
                "vision/computer-use model. The agent requires a Fara-class vision model."
            )

        # Bound memory: drop terminal sessions older than 15 minutes (their
        # browser contexts are already closed; this trims the bookkeeping).
        now = time.time()
        for sid, s in list(sessions.items()):
            if s.loop.state in (AgentState.COMPLETED, AgentState.ABORTED, AgentState.FAILED) \
                    and now - s.created_at > 900:
                sessions.pop(sid, None)

        loop = _new_loop(goal, body)
        # Wire the loop's on_event callback to the session queue.
        session = AgentSession(loop)

        async def _on_event(event_type: str, data: Dict[str, Any]) -> None:
            await session.emit(event_type, data)

        loop.on_event = _on_event
        sessions[session.id] = session

        return {
            "session_id": session.id,
            "goal": goal,
            "allowlist": body.get("allowlist") or [],
            "state": loop.state.value,
        }

    @router.post("/sessions/{session_id}/run")
    async def run_session(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.task and not session.task.done():
            raise HTTPException(status_code=409, detail="Session already running")

        _start_loop_task(session)
        await session.emit("run_started", {})
        return {"session_id": session_id, "state": session.loop.state.value}

    @router.post("/sessions/{session_id}/pause")
    async def pause_session(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.loop.safety.pause("Paused by user")
        session.loop.state = AgentState.PAUSED
        await session.emit("paused", {})
        return {"session_id": session_id, "state": session.loop.state.value}

    @router.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str, body: Optional[Dict[str, Any]] = None):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        answer = (body or {}).get("answer", "")
        await session.loop.resume(answer)
        await session.emit("resumed", {"answer": answer})
        # Restart the loop task only for a genuinely paused session — never
        # for a terminal one (resume() no-ops there; a new run() task would
        # be a zombie that exits immediately).
        if session.loop.state in (AgentState.PAUSED, AgentState.WAITING_FOR_USER) \
                and (not session.task or session.task.done()):
            _start_loop_task(session)
        return {"session_id": session_id, "state": session.loop.state.value}

    @router.post("/sessions/{session_id}/answer")
    async def answer_session(session_id: str, body: Optional[Dict[str, Any]] = None):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        answer = (body or {}).get("answer", "")
        await session.loop.resume(answer)
        await session.emit("answered", {"answer": answer})
        # Same guard as resume: only restart for a genuinely paused session.
        if session.loop.state in (AgentState.PAUSED, AgentState.WAITING_FOR_USER) \
                and (not session.task or session.task.done()):
            _start_loop_task(session)
        return {"session_id": session_id, "state": session.loop.state.value}

    @router.post("/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        await session.loop.stop()
        await session.loop.close()
        await session.emit("aborted", {})
        return {"session_id": session_id, "state": session.loop.state.value}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session.loop.get_state()

    @router.get("/sessions/{session_id}/events")
    async def session_events(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        async def event_generator():
            # Send current state immediately
            yield f"data: {json.dumps({'type': 'state', **session.loop.get_state()})}\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(session.queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                        if event.get("type") in ("completed", "aborted", "failed"):
                            break
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream; charset=utf-8",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["create_agent_router"]