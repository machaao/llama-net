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
    elif name in ("pause_and_memorize_fact", "ask_user_question", "terminate", "top_level_memory"):
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
        self.system_prompt = build_system_prompt(allowlist or [], goal=goal)

        self.state = AgentState.IDLE
        self.start_url = start_url

        # Trajectory: interleaved history of (role, content dict).
        self.history: List[Dict[str, Any]] = []
        self.screenshot_history: List[str] = []
        self.steps_completed = 0
        self.final_summary = ""
        self._last_error = ""
        self._parse_retries = 0          # number of consecutive parse failures
        self.MAX_PARSE_RETRIES = 2       # corrective nudge attempts before failing

        # Repeated-action detection (breaks no-effect click loops)
        self._last_action_key: Optional[str] = None
        self._repeat_count = 0
        self.MAX_IDENTICAL_REPEATS = 3

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

    def _build_messages(self, screenshot: str, url: str = "", title: str = "") -> List[Dict[str, Any]]:
        """Assemble the multimodal message list for the model.

        The current page URL and title are included as *text* alongside the
        screenshot. This grounds the model even when the screenshot is blank or
        vision is degraded — without it, a model that receives no usable visual
        signal reports "the page did not visibly load" and flails (retrying
        http://, falling back to web_search) instead of acting on the page.
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]

        # Trajectory history (thoughts + actions as plain-text user/assistant turns).
        for turn in self.history:
            messages.append(turn)

        # Current user turn: goal + page context + latest screenshot.
        page_context = f"Current page URL: {url or 'about:blank'}"
        if title:
            page_context += f"\nCurrent page title: {title}"
        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": f"Goal: {self.goal}\n{page_context}\nWhat is the next action?"},
        ]
        if screenshot:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": screenshot},
            })
        messages.append({"role": "user", "content": content_parts})
        logger.debug(
            f"📝 Built {len(messages)} messages; text grounding sent to model:\n"
            f"Goal: {self.goal}\n{page_context}"
        )
        return messages

    # ── Core loop ────────────────────────────────────────────────────────

    @staticmethod
    def _action_key(action: AgentAction) -> str:
        """Stable key for repeat detection — coordinates rounded to tolerate
        ±4px jitter so (827,36) and (829,36) count as the same click."""
        args = action.arguments
        parts = [action.name]
        for k in sorted(args):
            v = args[k]
            if k in ("x", "y") and isinstance(v, (int, float)):
                v = int(round(v / 4)) * 4
            parts.append(f"{k}={v}")
        return "|".join(parts)

    async def start(self) -> None:
        """Initialize the browser and (optionally) navigate to the start URL."""
        if self.state is not AgentState.IDLE:
            return
        try:
            await self.browser.start(start_url=self.start_url)
            # If a start URL was provided, let it finish rendering before the
            # first screenshot so the model sees real content, not a blank page.
            if self.start_url:
                await self.browser.wait_for_settle()
        except Exception as e:
            logger.error(f"Browser session failed to start: {e}")
            self.state = AgentState.FAILED
            await self._emit(
                "execution_error",
                error=f"Browser failed to start: {e}",
                action={"name": "browser_start", "arguments": {}},
            )
            return
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

        # Capture page context as text so the model is grounded even when the
        # screenshot is blank or vision is degraded.
        try:
            current_url = await self.browser.current_url()
            current_title = await self.browser.current_title()
        except Exception:
            current_url, current_title = "", ""

        logger.info(
            f"🔎 Step {self.steps_completed + 1}: url={current_url or 'about:blank'} "
            f"title={current_title!r} screenshot={'yes' if screenshot else 'NO'} "
            f"({len(screenshot)} chars)"
        )

        await self._emit(
            "step_screenshot",
            index=self.steps_completed + 1,
            screenshot=screenshot,
            url=current_url,
            title=current_title,
        )

        messages = self._build_messages(screenshot, url=current_url, title=current_title)
        # Log the exact message shape sent to the model so we can confirm the
        # image part is present and vision is actually engaged.
        last_user = messages[-1] if messages else {}
        last_content = last_user.get("content")
        if isinstance(last_content, list):
            part_types = [p.get("type") for p in last_content]
            logger.info(
                f"🧠 Sending {len(messages)} messages to model; "
                f"last user content parts={part_types}; "
                f"vision={'on' if getattr(self.llm, 'supports_vision', False) else 'OFF'}"
            )
        else:
            logger.warning(
                f"🧠 Sending {len(messages)} messages; last user content is "
                f"{type(last_content).__name__} (no image part!)"
            )

        result = await self.llm.generate_chat_multimodal(
            messages=messages,
            temperature=0.3,
            top_p=0.9,
            stop=["</tool_call>", "<|im_end|>"],
        )
        raw_text = result.get("content", "") or result.get("text", "")
        reasoning = result.get("reasoning", "")

        logger.info(f"🤖 Model output ({len(raw_text)} chars): {raw_text[:300]!r}")

        await self._emit("model_response", reasoning=reasoning, raw=raw_text)

        # Parse (fail closed on malformed output)
        try:
            cot, action = ToolCallParser.parse(raw_text)
        except ToolCallParseError as e:
            self._parse_retries += 1
            message = f"Failed to parse tool call: {e}"
            logger.warning(message)
            # Surface the raw model output so the operator can diagnose the
            # actual format the (quantized) model is emitting.
            logger.warning(f"Raw model output (first 2000 chars):\n{raw_text[:2000]}")

            self.history.append({"role": "assistant", "content": raw_text})

            if self._parse_retries < self.MAX_PARSE_RETRIES:
                # Give the model a corrective nudge and retry rather than
                # immediately failing — quantized models often drift from the
                # exact tool-call format on the first attempt.
                logger.info(
                    f"🔁 Parse retry {self._parse_retries}/{self.MAX_PARSE_RETRIES} — "
                    f"nudging model to emit a valid tool call"
                )
                self.history.append({
                    "role": "user",
                    "content": (
                        "Your last response did not contain a valid tool call. "
                        "Respond with EXACTLY one action in a single "
                        "<tool_call>{\"name\": \"computer_use\", \"arguments\": "
                        "{\"action\": \"<action_name>\", ...}}</tool_call> "
                        "block, where arguments.action is one of the action "
                        "names from the schema (e.g. {\"action\": \"visit_url\", "
                        "\"url\": \"https://...\"}). Do not add extra prose."
                    ),
                })
                await self._emit("parse_retry", attempt=self._parse_retries, error=str(e))
                # Do not change state to FAILED — let run() call step() again.
                return

            # Exhausted retries — fail closed.
            self.history.append({
                "role": "user",
                "content": message + " Please emit a valid <tool_call> block.",
            })
            self.state = AgentState.FAILED
            await self._emit("parse_error", error=str(e), raw=raw_text[:2000])
            return

        if reasoning:
            cot = reasoning or cot
        await self._emit("thinking", content=cot, action=action.to_dict())

        # Reset parse-retry counter on a successful parse.
        self._parse_retries = 0

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
            # Let the page finish loading/rendering before the next screenshot.
            # Without this, the next step captures a blank/loading page and the
            # model wrongly concludes the navigation failed.
            await self.browser.wait_for_settle()
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            self.state = AgentState.FAILED
            await self._emit("execution_error", error=str(e), action=action.to_dict())
            return

        # ── Repeated-action detection ──
        # A quantized 9B model can loop on the same no-effect click. Detect
        # identical consecutive actions (with coordinate tolerance) and inject
        # a corrective nudge so it tries a different approach.
        action_key = self._action_key(action)
        if action_key == self._last_action_key:
            self._repeat_count += 1
        else:
            self._last_action_key = action_key
            self._repeat_count = 0

        self.steps_completed += 1
        self.history.append({"role": "assistant", "content": raw_text})

        if self._repeat_count >= self.MAX_IDENTICAL_REPEATS:
            # Same action repeated with no effect — break the loop.
            nudge = (
                f"The action {action.name} {action.arguments} has been repeated "
                f"{self._repeat_count + 1} times with no effect (URL still "
                f"{current_url or 'about:blank'}). Do NOT repeat the same "
                f"coordinates. Try different coordinates, or use visit_url / "
                f"web_search to reach the target directly."
            )
            self.history.append({"role": "user", "content": nudge})
            await self._emit(
                "action_repeat_warning",
                count=self._repeat_count + 1,
                action=action.to_dict(),
                url=current_url,
            )
            self._repeat_count = 0
            self._last_action_key = None
        else:
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
        """Run the loop until a terminal state or pause.

        Any unexpected exception is surfaced as a ``failed`` event and sets the
        session to ``FAILED`` — never silently swallowed, so the UI always sees
        *something* (an error) instead of a dead background task.
        """
        try:
            await self.start()
            while self.state in (AgentState.RUNNING,):
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Agent step failed: {e}", exc_info=True)
                    self.state = AgentState.FAILED
                    self._last_error = str(e)
                    await self._emit("execution_error", error=str(e))
                    break
        except asyncio.CancelledError:
            self.state = AgentState.ABORTED
            await self._emit("aborted")
            raise
        except Exception as e:
            logger.error(f"Agent run failed: {e}", exc_info=True)
            self.state = AgentState.FAILED
            self._last_error = str(e)
            await self._emit("execution_error", error=str(e))
        finally:
            # Emit a terminal 'failed' event if we ended in a failed state
            # without a cleaner terminal signal, so the UI status badge and
            # controls update consistently.
            if self.state is AgentState.FAILED:
                await self._emit("failed", error=self._last_error or "Agent failed")

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