"""The Fara1.5-9B computer-use agent loop.

Orchestrates the screenshot → model → parse → execute cycle, enforcing safety
controls before each action and recording a full trajectory trace.

The loop is deliberately decoupled from the HTTP layer: it exposes
``step()``/``run()`` so the FastAPI router (``agent.server``) can drive it in a
background task while consuming per-step SSE events.
"""

import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional

from common.utils import get_logger
from agent.actions import AgentAction
from agent.browser_session import BrowserSession
from agent.parser import ToolCallParser, ToolCallParseError
from agent.prompts import build_system_prompt
from agent.safety import SafetyController, SafetyViolation

logger = get_logger(__name__)


class AgentState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


# ── Action → Playwright executor ─────────────────────────────────────────
async def execute_action(page, action: AgentAction) -> Dict[str, Any]:
    """Execute a validated action against the Playwright page.

    Returns a small result dict for the trace log. Raises on failure so the
    loop can record an error and continue (or abort as configured).
    """
    name = action.name
    args = action.arguments

    if name == "left_click":
        await page.mouse.click(args["x"], args["y"], button="left")
    elif name == "right_click":
        await page.mouse.click(args["x"], args["y"], button="right")
    elif name == "double_click":
        await page.mouse.dblclick(args["x"], args["y"])
    elif name == "triple_click":
        await page.mouse.click(args["x"], args["y"], click_count=3)
    elif name == "type":
        await page.keyboard.type(args["text"])
    elif name == "scroll":
        await page.mouse.wheel(0, args["delta"])
    elif name == "hscroll":
        await page.mouse.wheel(args["delta"], 0)
    elif name == "visit_url":
        await page.goto(args["url"], wait_until="domcontentloaded")
    elif name == "web_search":
        from urllib.parse import quote
        url = f"https://www.google.com/search?q={quote(args['query'])}"
        await page.goto(url, wait_until="domcontentloaded")
    elif name == "history_back":
        await page.go_back(wait_until="domcontentloaded")
    elif name in ("pause_and_memorize_fact", "ask_user_question", "terminate"):
        # Control actions — no browser operation; handled by the loop/safety.
        pass
    else:
        raise ValueError(f"Unsupported action for execution: {name}")

    return {"action": name, "arguments": args}


class AgentLoop:
    """Drives the computer-use agent loop for a single session."""

    # Keep the most recent N screenshots in the trajectory context.
    MAX_SCREENSHOT_HISTORY = 5
    DEFAULT_MAX_STEPS = 30

    def __init__(
        self,
        llm,
        goal: str,
        allowlist: Optional[List[str]] = None,
        blocklist: Optional[List[str]] = None,
        start_url: Optional[str] = None,
        headless: bool = True,
        max_steps: int = DEFAULT_MAX_STEPS,
        on_event: Optional[Any] = None,
    ):
        """
        Args:
            llm: A ``LlamaWrapper`` instance — must expose
                ``generate_chat_multimodal(messages, ...)``.
            goal: The end-to-end task description.
            allowlist: Optional domains navigation is restricted to.
            blocklist: Optional domains to always block.
            start_url: Optional initial URL to navigate to.
            headless: Run the browser headless (default True).
            max_steps: Maximum number of actions before stopping.
            on_event: Optional async callback(event_type, data) for SSE/UI.
        """
        self.llm = llm
        self.goal = goal
        self.max_steps = max_steps
        self.on_event = on_event

        self.browser = BrowserSession(headless=headless)
        self.safety = SafetyController(allowlist=allowlist, blocklist=blocklist)
        self.system_prompt = build_system_prompt(allowlist or [])

        self.state = AgentState.IDLE
        self.start_url = start_url

        # Trajectory: interleaved history of (role, content dict).
        self.history: List[Dict[str, Any]] = []
        self.screenshot_history: List[str] = []
        self.steps_completed = 0
        self.final_summary = ""

        self._task: Optional[asyncio.Task] = None

    # ── Event helpers ───────────────────────────────────────────────────

    async def _emit(self, event_type: str, **data: Any) -> None:
        self.safety.record(event_type, data)
        if self.on_event:
            try:
                await self.on_event(event_type, data)
            except Exception as e:
                logger.debug(f"Agent event callback error: {e}")

    # ── Message construction ─────────────────────────────────────────────

    def _build_messages(self, screenshot: str) -> List[Dict[str, Any]]:
        """Assemble the multimodal message list for the model."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]

        # Trajectory history (thoughts + actions as plain-text user/assistant turns).
        for turn in self.history:
            messages.append(turn)

        # Current user turn: goal + latest screenshot(s).
        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": f"Goal: {self.goal}\nWhat is the next action?"},
        ]
        if screenshot:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": screenshot},
            })
        messages.append({"role": "user", "content": content_parts})
        return messages

    # ── Core loop ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the browser and (optionally) navigate to the start URL."""
        if self.state is not AgentState.IDLE:
            return
        await self.browser.start(start_url=self.start_url)
        self.state = AgentState.RUNNING
        await self._emit("session_started", goal=self.goal)

    async def step(self) -> None:
        """Perform exactly one screenshot → model → parse → execute cycle."""
        if self.state not in (AgentState.RUNNING,):
            return

        screenshot = await self.browser.screenshot()
        self.screenshot_history.append(screenshot)
        if len(self.screenshot_history) > self.MAX_SCREENSHOT_HISTORY:
            self.screenshot_history = self.screenshot_history[-self.MAX_SCREENSHOT_HISTORY:]

        await self._emit(
            "step_screenshot",
            index=self.steps_completed + 1,
            screenshot=screenshot,
        )

        messages = self._build_messages(screenshot)
        result = await self.llm.generate_chat_multimodal(
            messages=messages,
            max_tokens=2048,
            temperature=0.3,
            top_p=0.9,
            stop=["</tool_call>", "<|im_end|>"],
        )
        raw_text = result.get("content", "") or result.get("text", "")
        reasoning = result.get("reasoning", "")

        await self._emit("model_response", reasoning=reasoning, raw=raw_text)

        # Parse (fail closed on malformed output)
        try:
            cot, action = ToolCallParser.parse(raw_text)
        except ToolCallParseError as e:
            message = f"Failed to parse tool call: {e}"
            logger.warning(message)
            self.history.append({"role": "assistant", "content": raw_text})
            self.history.append({
                "role": "user",
                "content": message + " Please emit a valid <tool_call> block.",
            })
            self.state = AgentState.FAILED
            await self._emit("parse_error", error=str(e))
            return

        if reasoning:
            cot = reasoning or cot
        await self._emit("thinking", content=cot, action=action.to_dict())

        # Safety gate (may pause)
        try:
            self.safety.check_step(action)
        except SafetyViolation as e:
            logger.warning(f"Safety violation: {e}")
            self.state = AgentState.PAUSED
            self.safety.pause(str(e))
            await self._emit("safety_blocked", reason=str(e), action=action.to_dict())
            return

        # Control actions that terminate or pause
        if action.name == "terminate":
            self.final_summary = action.arguments.get("summary", "")
            self.state = AgentState.COMPLETED
            self.history.append({"role": "assistant", "content": raw_text})
            await self._emit("completed", summary=self.final_summary)
            return

        if self.safety.paused:
            self.state = AgentState.WAITING_FOR_USER
            await self._emit(
                "waiting_for_user",
                question=self.safety.pause_reason,
                action=action.to_dict(),
            )
            return

        # Execute the action
        try:
            exec_result = await execute_action(self.browser.page, action)
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            self.state = AgentState.FAILED
            await self._emit("execution_error", error=str(e), action=action.to_dict())
            return

        self.steps_completed += 1
        self.history.append({"role": "assistant", "content": raw_text})
        # Note the action just taken (as a short user-turn so the model sees it).
        self.history.append({
            "role": "user",
            "content": f"Executed action: {action.name} {action.arguments}",
        })

        await self._emit("action_executed", result=exec_result, step=self.steps_completed)

        if self.steps_completed >= self.max_steps:
            self.state = AgentState.COMPLETED
            self.final_summary = f"Reached maximum step limit ({self.max_steps})."
            await self._emit("completed", summary=self.final_summary)

    async def run(self) -> None:
        """Run the loop until a terminal state or pause."""
        await self.start()
        while self.state in (AgentState.RUNNING,):
            await self.step()

    # ── Control API for the router ──────────────────────────────────────

    async def resume(self, answer: str = "") -> None:
        if self.state not in (AgentState.PAUSED, AgentState.WAITING_FOR_USER):
            return
        if answer:
            self.history.append({"role": "user", "content": answer})
        self.safety.resume(answer)
        self.state = AgentState.RUNNING
        await self._emit("resumed", answer=answer)

    async def confirm_irreversible(self) -> None:
        self.safety.authorize_once()
        if self.state is AgentState.PAUSED:
            self.state = AgentState.RUNNING
        await self._emit("authorized_once")

    async def stop(self) -> None:
        self.safety.abort()
        self.state = AgentState.ABORTED
        if self._task and not self._task.done():
            self._task.cancel()
        await self._emit("aborted")

    async def close(self) -> None:
        await self.browser.close()

    def get_state(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "goal": self.goal,
            "steps_completed": self.steps_completed,
            "final_summary": self.final_summary,
            "paused": self.safety.paused,
            "pause_reason": self.safety.pause_reason,
            "trace": self.safety.get_trace(),
        }


__all__ = [
    "AgentLoop",
    "AgentState",
    "execute_action",
]