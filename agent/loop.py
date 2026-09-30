"""The Fara1.5-9B computer-use agent loop.

Orchestrates the screenshot → model → parse → execute cycle, enforcing safety
controls before each action and recording a full trajectory trace.

The loop is deliberately decoupled from the HTTP layer: it exposes
``step()``/``run()`` so the FastAPI router (``agent.server``) can drive it in a
background task while consuming per-step SSE events.
"""

import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from common.utils import get_logger
from agent.actions import AgentAction
from agent.browser_session import BrowserSession
from agent.parser import ToolCallParser, ToolCallParseError
from json_repair import loads as repair_json_loads

from agent.prompts import build_system_prompt, PLAN_INSTRUCTIONS
from agent.safety import SafetyController, SafetyViolation

logger = get_logger(__name__)


def _truncate_history_text(text: str, limit: int) -> str:
    """Tail-preserving truncation for text kept in the trajectory history.

    The tail holds the tool call — the part the model needs for continuity —
    while the long chain-of-thought head is safe to drop.
    """
    if not text or len(text) <= limit:
        return text
    return "[earlier text truncated] " + text[-(limit - 26):]


def _to_model_space(x: int, y: int, viewport: Dict[str, Any]) -> Tuple[int, int]:
    """Viewport pixels → the model's 0-1000 relative coordinate space."""
    vw = int(viewport.get("width") or 1280)
    vh = int(viewport.get("height") or 800)
    return round(x * 1000 / vw), round(y * 1000 / vh)


def _rescale_pointer_args(action: AgentAction, viewport: Dict[str, Any]) -> None:
    """Map pointer coordinates from the model's 0-1000 relative space to
    viewport pixels, in place.

    Fara emits coordinates on a 0-1000 scale relative to the screenshot (its
    trained coordinate space) — verified empirically: a click aimed at a nav
    link centered at viewport (520, 35) arrives as (406, 44) =
    520×1000/1280, 35×1000/800. Treating those values as viewport pixels
    clicks the element one slot to the left of the intended target.
    """
    vw = int(viewport.get("width") or 1280)
    vh = int(viewport.get("height") or 800)
    x = action.arguments.get("x")
    y = action.arguments.get("y")
    if x is None or y is None:
        return
    action.arguments["x"] = max(0, min(vw - 1, round(float(x) * vw / 1000)))
    action.arguments["y"] = max(0, min(vh - 1, round(float(y) * vh / 1000)))


def _format_nearby_elements(
    elements: List[Dict[str, Any]],
    viewport: Dict[str, Any],
    limit: int = 6,
) -> str:
    """Format nearby interactive elements for feedback, in the model's
    0-1000 coordinate space (the space it emits clicks in)."""
    vw = int(viewport.get("width") or 1280)
    vh = int(viewport.get("height") or 800)
    lines = []
    for el in elements[:limit]:
        r = el.get("rect") or {}
        x1 = round((r.get("x") or 0) * 1000 / vw)
        y1 = round((r.get("y") or 0) * 1000 / vh)
        x2 = round(((r.get("x") or 0) + (r.get("w") or 0)) * 1000 / vw)
        y2 = round(((r.get("y") or 0) + (r.get("h") or 0)) * 1000 / vh)
        text = (el.get("text") or "")[:30]
        lines.append(f"<{el.get('tag')}> '{text}' [{x1},{y1} → {x2},{y2}]")
    return "; ".join(lines)


class AgentState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


# ── Action → Playwright executor ─────────────────────────────────────────
async def execute_action(browser, action: AgentAction) -> Dict[str, Any]:
    """Execute a validated action against the browser session.

    Pointer actions go through ``BrowserSession.click_at`` (Playwright native
    element click with hit-target verification) and scroll actions through
    ``BrowserSession.scroll_by`` (center-parked wheel + movement verification).
    Other actions use the raw page directly.

    Returns a result dict for the trace log. Raises on failure so the
    loop can record an error and continue (or abort as configured).
    """
    name = action.name
    args = action.arguments
    page = browser.page

    # ── Pointer: native click with hit-target check ──
    if name in ("left_click", "right_click", "double_click", "triple_click"):
        button = "right" if name == "right_click" else "left"
        click_count = {"double_click": 2, "triple_click": 3}.get(name, 1)
        result = await browser.click_at(
            args["x"], args["y"], button=button, click_count=click_count,
        )
        return {"action": name, "arguments": args, **result}

    # ── Scroll: center-parked wheel with movement verification ──
    if name in ("scroll", "hscroll"):
        dx = args["delta"] if name == "hscroll" else 0
        dy = args["delta"] if name == "scroll" else 0
        result = await browser.scroll_by(dx, dy)
        return {"action": name, "arguments": args, **result}

    # ── Keyboard / navigation / control ──
    if name == "type":
        await page.keyboard.type(args["text"])
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

    # Sliding window of trajectory turns sent to the model. Re-sending the
    # FULL history every step re-prefills O(N²) tokens over a run — the
    # dominant compute cost on 16-24 GB machines (vision prefill included).
    MAX_HISTORY_TURNS = 12
    # Prior-turn text kept in history (tail-preserving; see _truncate_history_text).
    MAX_HISTORY_TEXT = 600

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

        # Ordered plan for the goal — advisory grounding, not a safety gate.
        # Seeded with the goal; replaced by a decomposed plan in _create_plan().
        self.plan: List[str] = [goal]
        self.plan_index: int = 0

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
        # Record without bulky payloads — base64 screenshots are delivered
        # live via SSE; keeping them in the trace grows sessions to MBs and
        # bloats GET /agent/sessions/{id} responses.
        self.safety.record(
            event_type, {k: v for k, v in data.items() if k != "screenshot"}
        )
        if self.on_event:
            try:
                await self.on_event(event_type, data)
            except Exception as e:
                logger.debug(f"Agent event callback error: {e}")

    def _remember(self, role: str, content: str) -> None:
        """Append a trajectory turn with bounded text (see MAX_HISTORY_TEXT)."""
        self.history.append({
            "role": role,
            "content": _truncate_history_text(content, self.MAX_HISTORY_TEXT),
        })

    # ── Plan construction ────────────────────────────────────────────────

    async def _create_plan(self) -> None:
        """Decompose the goal into an ordered plan and append it to the prompt.

        Advisory grounding only — any failure falls back to the goal as a
        single-step plan so the session never blocks on planning.
        """
        steps: List[str] = []
        try:
            result = await self.llm.generate_chat_safe(
                messages=[
                    {"role": "system", "content": (
                        "You break a browser-automation goal into 2-6 short "
                        "imperative steps. Reply with ONLY a JSON array of "
                        "strings, no prose."
                    )},
                    {"role": "user", "content": f"Goal: {self.goal}"},
                ],
                max_tokens=400,
                temperature=0.2,
            )
            raw = (result.get("content") or result.get("text") or "").strip()
            start, end = raw.find("["), raw.rfind("]")
            if start != -1 and end > start:
                parsed = repair_json_loads(raw[start:end + 1])
                if isinstance(parsed, list):
                    steps = [str(s).strip() for s in parsed if str(s).strip()]
        except Exception as e:
            logger.warning(f"Plan creation failed, using goal as single step: {e}")

        self.plan = steps[:6] or [self.goal]
        self.plan_index = 0
        self.system_prompt = (
            self.system_prompt
            + "\n\n" + PLAN_INSTRUCTIONS
            + "Plan:\n"
            + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(self.plan))
        )
        await self._emit("plan_created", steps=self.plan)

    # ── Message construction ─────────────────────────────────────────────

    def _build_messages(
        self,
        screenshot: str,
        url: str = "",
        title: str = "",
        scroll: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
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

        # Trajectory history — sliding window only. The full trajectory grows
        # without bound; re-prefilling all of it every step is what overwhelms
        # a local 9B vision model on long runs.
        for turn in self.history[-self.MAX_HISTORY_TURNS:]:
            messages.append(turn)

        # Current user turn: goal + page context + latest screenshot.
        page_context = f"Current page URL: {url or 'about:blank'}"
        if title:
            page_context += f"\nCurrent page title: {title}"
        # Ground the coordinate space explicitly — a 9B model has no way to
        # infer it from the image alone, and a wrong assumption here is the
        # root cause of systematically off-target clicks.
        vp = getattr(self.browser, "viewport", None) or {}
        if vp.get("width"):
            page_context += (
                f"\nViewport: {vp['width']}x{vp['height']} pixels. "
                f"Click coordinates are absolute viewport pixels. "
                f"The screenshot shows ONLY this visible viewport — click "
                f"coordinates MUST be within 0-{vp['width'] - 1} x "
                f"0-{vp['height'] - 1}. Content below the fold is not "
                f"visible; scroll first."
            )
        # Scroll grounding — the decisive facts the model needs to stop
        # probing up/down: position, max, bottom/top reached, sign convention.
        if scroll:
            y = int(scroll.get("y", 0))
            max_y = int(scroll.get("max_y", 0))
            pct = int((y / max_y) * 100) if max_y > 0 else 0
            page_context += (
                f"\nScroll position: y={y} of {max_y} px ({pct}% from top; "
                f"positive delta scrolls down)"
            )
            if scroll.get("at_bottom"):
                page_context += "\nPage is at the BOTTOM — no more content below."
            elif scroll.get("at_top"):
                page_context += "\nPage is at the TOP."

        # Plan block — keeps the model oriented in multi-phase goals.
        plan_lines = []
        for i, step in enumerate(self.plan):
            if i < self.plan_index:
                marker = "[done]"
            elif i == self.plan_index:
                marker = "[CURRENT]"
            else:
                marker = "[ ]"
            plan_lines.append(f"  {i + 1}. {marker} {step}")
        plan_block = (
            f"Goal: {self.goal}\n"
            f"Plan:\n" + "\n".join(plan_lines) + "\n"
            f"Current plan step: {self.plan_index + 1} of {len(self.plan)} "
            f"— {self.plan[self.plan_index]}\n"
        )

        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": f"{plan_block}{page_context}\nWhat is the next action?"},
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
        await self._create_plan()
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

        try:
            scroll = await self.browser.current_scroll()
        except Exception:
            scroll = {}

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
            scroll_y=scroll.get("y", 0),
            max_scroll_y=scroll.get("max_y", 0),
        )

        messages = self._build_messages(
            screenshot, url=current_url, title=current_title, scroll=scroll,
        )
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

            self._remember("assistant", raw_text)

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

        # ── Coordinate-space mapping ──
        # Fara emits pointer coordinates on a 0-1000 scale relative to the
        # screenshot, NOT viewport pixels. Rescale for execution; keep the
        # model-space values to echo back in feedback text.
        model_coords: Optional[Tuple[int, int]] = None
        if action.is_pointer:
            model_coords = (
                action.arguments.get("x"),
                action.arguments.get("y"),
            )
            _rescale_pointer_args(
                action, getattr(self.browser, "viewport", None) or {}
            )
            logger.debug(
                f"🖱 Pointer coords: model-space {model_coords} → "
                f"viewport ({action.arguments['x']}, {action.arguments['y']})"
            )

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
            self._remember("assistant", raw_text)
            await self._emit("completed", summary=self.final_summary)
            return

        if action.name == "complete_step":
            # Plan-advance control action — no browser operation.
            self._remember("assistant", raw_text)
            completed_index = self.plan_index
            completed_step = self.plan[completed_index]
            note = action.arguments.get("note", "")
            self.plan_index = min(completed_index + 1, len(self.plan) - 1)
            # Counts toward max_steps so complete_step spam can't loop forever.
            self.steps_completed += 1
            await self._emit(
                "plan_step_completed",
                index=completed_index + 1,
                completed_step=completed_step,
                note=note,
                remaining=self.plan[self.plan_index:] if self.plan_index < len(self.plan) else [],
            )
            if self.steps_completed >= self.max_steps:
                self.state = AgentState.COMPLETED
                self.final_summary = f"Reached maximum step limit ({self.max_steps})."
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
            exec_result = await execute_action(self.browser, action)
            # Let the page finish loading/rendering before the next screenshot.
            # Without this, the next step captures a blank/loading page and the
            # model wrongly concludes the navigation failed.
            await self.browser.wait_for_settle()
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            self.state = AgentState.FAILED
            await self._emit("execution_error", error=str(e), action=action.to_dict())
            return

        # ── Action feedback (click + scroll verification) ──
        # Two click failure modes:
        #   1. Playwright hit-target check failed (overlay intercepts) —
        #      exec_result carries clicked=False + intercepted flag.
        #   2. Click succeeded but landed on an inert element with URL
        #      unchanged — coordinates were wrong.
        # Scroll failure: wheel dispatched but scrollY didn't move.
        action_feedback: Optional[str] = None
        if action.is_pointer:
            px = action.arguments["x"]   # viewport pixels (post-rescale)
            py = action.arguments["y"]
            # Model-space coordinates for feedback text — echo what the model
            # emitted so its mental model and our feedback stay consistent.
            mx, my = model_coords if model_coords else (px, py)
            vp = getattr(self.browser, "viewport", None) or {}

            if not exec_result.get("clicked", True):
                element = exec_result.get("element") or {}
                err_text = exec_result.get("error", "") or ""

                if exec_result.get("out_of_bounds") or "outside viewport" in err_text:
                    # Mode 0: coordinates outside the viewport (and therefore
                    # outside the screenshot's coordinate space). The screenshot
                    # is viewport-only, so a point like (291, 983) in a
                    # 1280x800 viewport maps to nothing — a raw click would be
                    # a silent no-op. Direct the model to scroll first.
                    vp = getattr(self.browser, "viewport", None) or {}
                    vp_desc = f"{vp.get('width', '?')}x{vp.get('height', '?')}"
                    action_feedback = (
                        f"The click at ({px}, {py}) is OUTSIDE the {vp_desc} "
                        f"viewport — the screenshot shows only the visible "
                        f"area. If the target is below the fold, scroll down "
                        f"first (positive delta), then recompute coordinates "
                        f"from the new screenshot."
                    )
                    logger.warning(
                        f"🖱 Click out of bounds: ({px}, {py}) (viewport {vp_desc})"
                    )
                    await self._emit(
                        "click_missed",
                        x=px, y=py, element=element,
                        url=await self.browser.current_url(),
                        reason="out_of_bounds",
                        message=action_feedback,
                    )
                else:
                    # Mode 1: native click was blocked by an interceptor.
                    tag = element.get("tag", "unknown")
                    if exec_result.get("intercepted"):
                        action_feedback = (
                            f"The click at ({px}, {py}) was BLOCKED — <{tag}> "
                            f"(or an overlay above it) is intercepting pointer "
                            f"events. The page did not change. Try different "
                            f"coordinates, or use visit_url to navigate directly."
                        )
                    else:
                        action_feedback = (
                            f"The click at ({px}, {py}) failed on <{tag}>: "
                            f"{err_text or 'unknown error'}. "
                            f"Recompute coordinates from the screenshot."
                        )
                    logger.warning(
                        f"🖱 Click blocked: ({px}, {py}) → "
                        f"{'intercepted' if exec_result.get('intercepted') else err_text}"
                    )
                    await self._emit(
                        "click_missed",
                        x=px, y=py, element=element,
                        url=await self.browser.current_url(),
                        reason="intercepted" if exec_result.get("intercepted") else "error",
                        message=action_feedback,
                    )
            else:
                # Mode 2: click succeeded — verify the click had an effect.
                # A click on an interactive element that changes nothing (a
                # link to the current page, a target="_blank" handled
                # elsewhere, a JS handler that no-ops) previously produced NO
                # feedback at all — the model only learned the failure from
                # the next screenshot and repeated the same click.
                try:
                    url_after = await self.browser.current_url()
                    element = await self.browser.element_at(px, py)
                    tag = (element or {}).get("tag", "")
                    interactive = bool(element and element.get("interactive"))
                    el_text = ((element or {}).get("text") or "").strip()
                    el_href = ((element or {}).get("href") or "").strip()
                    rect = (element or {}).get("rect") or {}
                    hit_desc = f"<{tag}>" + (f" '{el_text[:40]}'" if el_text else "")
                    if rect:
                        bx1, by1 = _to_model_space(
                            rect.get("x") or 0, rect.get("y") or 0, vp
                        )
                        bx2, by2 = _to_model_space(
                            (rect.get("x") or 0) + (rect.get("w") or 0),
                            (rect.get("y") or 0) + (rect.get("h") or 0), vp,
                        )
                        hit_desc += f" [box {bx1},{by1} → {bx2},{by2}]"
                    # What else is clickable near the click point — lets the
                    # model pivot to the intended element's exact coordinates
                    # instead of re-guessing from the screenshot (whose
                    # coordinate space may be scaled by the vision pipeline).
                    try:
                        nearby = await self.browser.interactive_elements_near(px, py)
                    except Exception:
                        nearby = []
                    nearby_txt = _format_nearby_elements(nearby, vp)
                    nearby_section = ""
                    if nearby_txt:
                        nearby_section = (
                            " Interactive elements near the click point "
                            "(0-1000 scale coordinates, the same space as "
                            "your click coordinates): " + nearby_txt +
                            ". If one of these is your target, click its "
                            "listed coordinates directly."
                        )

                    if exec_result.get("new_tab"):
                        action_feedback = (
                            f"The click at ({px}, {py}) opened a NEW TAB "
                            f"({url_after or 'about:blank'} is now the active "
                            f"page). Continue working in the new tab."
                        )
                    elif url_after == current_url and not interactive:
                        missed = True
                        action_feedback = (
                            f"The click at ({px}, {py}) landed on {hit_desc} "
                            f"— not an interactive element — and the page did "
                            f"not change (URL still {url_after or 'about:blank'}). "
                            f"The coordinates are likely wrong. Recompute the "
                            f"target position from the screenshot, or use "
                            f"visit_url to navigate directly."
                            + nearby_section
                        )
                        logger.warning(
                            f"🖱 Click missed: ({px}, {py}) → <{tag or 'none'}> "
                            f"(inert, URL unchanged)"
                        )
                        await self._emit(
                            "click_missed",
                            x=px, y=py, element=element, url=url_after,
                            reason="inert_element", nearby=nearby,
                            message=action_feedback,
                        )
                    elif url_after == current_url:
                        # Interactive element, but the page did not change.
                        # Tell the model exactly what it hit and what the
                        # element points to — a link to the current URL or a
                        # no-op handler is the usual cause.
                        action_feedback = (
                            f"Clicked {hit_desc} at ({px}, {py}) — the page "
                            f"did not change (URL still {url_after or 'about:blank'})."
                        )
                        if el_href and el_href != url_after:
                            action_feedback += (
                                f" The element links to {el_href} — use "
                                f"visit_url with that URL to navigate directly."
                            )
                        action_feedback += nearby_section
                        logger.warning(
                            f"🖱 Click had no effect: ({px}, {py}) → {hit_desc} "
                            f"(URL unchanged)"
                        )
                        await self._emit(
                            "click_missed",
                            x=px, y=py, element=element, url=url_after,
                            reason="no_effect", nearby=nearby,
                            message=action_feedback,
                        )
                except Exception as e:
                    logger.debug(f"Click verification failed: {e}")

        # ── Scroll verification ──
        # Report scroll results on SUCCESS too — position, max, bottom-reached
        # and the sign convention are the decisive facts that stop the model
        # from probing up/down.
        if action.is_scroll:
            delta = action.arguments.get("delta", 0)
            if exec_result.get("scrolled", True):
                if action.name == "scroll":
                    direction = "DOWN" if delta >= 0 else "UP"
                else:
                    direction = "RIGHT" if delta >= 0 else "LEFT"
                action_feedback = (
                    f"Scrolled {direction} by {delta}px: scrollY "
                    f"{exec_result.get('scroll_y_before', 0)} → "
                    f"{exec_result.get('scroll_y', 0)} (max "
                    f"{exec_result.get('max_scroll_y', 0)}px). "
                    f"Positive delta scrolls DOWN."
                )
                if exec_result.get("at_bottom"):
                    action_feedback += (
                        " Page is at the BOTTOM — no more content below; "
                        "the scroll goal is achieved."
                    )
            elif not exec_result.get("can_scroll", True):
                action_feedback = (
                    "The page has no more content to scroll — it fits the "
                    "viewport. Use visit_url or history_back to navigate instead."
                )
            else:
                action_feedback = (
                    f"The scroll by {delta}px did not move the page (scrollY "
                    f"stayed at {exec_result.get('scroll_y', 0)}). Remember: "
                    f"positive delta scrolls DOWN, negative scrolls UP. An "
                    f"overlay may also be intercepting the wheel event."
                )
            if not exec_result.get("scrolled", True):
                logger.warning(
                    f"📜 Scroll had no effect: delta={delta} "
                    f"scrollY={exec_result.get('scroll_y', 0)} "
                    f"can_scroll={exec_result.get('can_scroll', '?')}"
                )
            await self._emit("action_feedback", action=action.to_dict(), message=action_feedback)

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
        self._remember("assistant", raw_text)

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
        elif action_feedback:
            # Immediate miss/scroll feedback — don't wait for the repeat threshold.
            self.history.append({"role": "user", "content": action_feedback})
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

            # Terminal states: tear down this session's browser context so the
            # next goal starts clean. The shared Chromium singleton stays up;
            # only this session's isolated context closes. Paused sessions
            # (WAITING_FOR_USER) keep the browser open for resume.
            if self.state in (AgentState.COMPLETED, AgentState.ABORTED, AgentState.FAILED):
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.debug(f"Browser teardown after terminal state failed: {e}")

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
            "plan": self.plan,
            "plan_index": self.plan_index,
            "trace": self.safety.get_trace(),
        }


__all__ = [
    "AgentLoop",
    "AgentState",
    "execute_action",
]