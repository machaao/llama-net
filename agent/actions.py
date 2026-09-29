"""Action schema and argument parsing for Fara1.5-9B tool calls.

Fara emits a chain-of-thought block followed by an XML ``<tool_call>`` block
containing a JSON object for the ``computer_use`` function. This module defines
the canonical action set and the argument validator used by the parser and the
executor.

Action set (from the Fara1.5-9B model card / MagenticLite harness):
  left_click, right_click, double_click, triple_click, type, scroll, hscroll,
  visit_url, web_search, history_back, pause_and_memorize_fact,
  ask_user_question, terminate.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Canonical action names ────────────────────────────────────────────────
POINTER_ACTIONS = {"left_click", "right_click", "double_click", "triple_click"}
KEYBOARD_ACTIONS = {"type"}
SCROLL_ACTIONS = {"scroll", "hscroll"}
NAVIGATION_ACTIONS = {"visit_url", "web_search", "history_back"}
CONTROL_ACTIONS = {
    "pause_and_memorize_fact",
    "ask_user_question",
    "terminate",
    "top_level_memory",
}

ALL_ACTION_NAMES = (
    POINTER_ACTIONS
    | KEYBOARD_ACTIONS
    | SCROLL_ACTIONS
    | NAVIGATION_ACTIONS
    | CONTROL_ACTIONS
)


@dataclass
class AgentAction:
    """A parsed, validated agent action ready for execution.

    Args:
        name: Canonical action name (one of ``ALL_ACTION_NAMES``).
        arguments: Raw argument dict (already schema-validated).
    """

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_pointer(self) -> bool:
        return self.name in POINTER_ACTIONS

    @property
    def is_keyboard(self) -> bool:
        return self.name in KEYBOARD_ACTIONS

    @property
    def is_scroll(self) -> bool:
        return self.name in SCROLL_ACTIONS

    @property
    def is_navigation(self) -> bool:
        return self.name in NAVIGATION_ACTIONS

    @property
    def is_control(self) -> bool:
        return self.name in CONTROL_ACTIONS

    @property
    def coordinates(self) -> Optional[Tuple[int, int]]:
        """Return (x, y) for pointer actions, else None."""
        if not self.is_pointer:
            return None
        x = self.arguments.get("x")
        y = self.arguments.get("y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


# ── Argument validation ──────────────────────────────────────────────────
class ActionValidationError(ValueError):
    """Raised when an action's arguments do not match the schema."""


def _require(args: Dict[str, Any], keys: List[str]) -> None:
    for key in keys:
        if key not in args or args[key] is None:
            raise ActionValidationError(f"Action requires argument '{key}'")


def parse_action_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an action's arguments against its schema.

    Returns the normalized argument dict (e.g. coordinates coerced to ints),
    or raises :class:`ActionValidationError` when invalid.
    """
    if name not in ALL_ACTION_NAMES:
        raise ActionValidationError(f"Unknown action '{name}'")

    if name in POINTER_ACTIONS:
        _require(arguments, ["x", "y"])
        try:
            x = int(arguments["x"])
            y = int(arguments["y"])
        except (TypeError, ValueError):
            raise ActionValidationError("Pointer action requires integer x/y coordinates")
        if x < 0 or y < 0:
            raise ActionValidationError("Pointer coordinates must be non-negative")
        return {"x": x, "y": y}

    if name == "type":
        _require(arguments, ["text"])
        text = arguments.get("text")
        if not isinstance(text, str):
            raise ActionValidationError("'type' action requires a string 'text'")
        return {"text": text}

    if name in SCROLL_ACTIONS:
        # Both accept a signed pixel delta; scroll is vertical, hscroll horizontal.
        _require(arguments, ["delta"])
        try:
            delta = int(arguments["delta"])
        except (TypeError, ValueError):
            raise ActionValidationError("Scroll action requires integer 'delta'")
        return {"delta": delta}

    if name == "visit_url":
        _require(arguments, ["url"])
        url = arguments.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ActionValidationError("'visit_url' requires an http(s) URL")
        return {"url": url}

    if name == "web_search":
        _require(arguments, ["query"])
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ActionValidationError("'web_search' requires a non-empty 'query'")
        return {"query": query.strip()}

    if name == "history_back":
        return {}

    if name == "pause_and_memorize_fact":
        _require(arguments, ["fact"])
        fact = arguments.get("fact")
        if not isinstance(fact, str):
            raise ActionValidationError("'pause_and_memorize_fact' requires a string 'fact'")
        return {"fact": fact}

    if name == "ask_user_question":
        _require(arguments, ["question"])
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ActionValidationError("'ask_user_question' requires a non-empty 'question'")
        return {"question": question.strip()}

    if name == "terminate":
        summary = arguments.get("summary", "")
        return {"summary": summary if isinstance(summary, str) else ""}

    if name == "top_level_memory":
        # Fara's memory action — accepts a free-form description/fact. Not a
        # browser operation; the loop records it and continues.
        text = arguments.get("description") or arguments.get("fact") or ""
        return {"description": text if isinstance(text, str) else str(text)}

    return dict(arguments)


# ── Schema description (for the agent system prompt) ───────────────────────
ACTION_SCHEMA: str = """Available tool calls (always emit exactly one inside a
single <tool_call> block with `name` = "computer_use"):

- left_click {x, y}          — left-click at pixel (x, y)
- right_click {x, y}         — right-click at pixel (x, y)
- double_click {x, y}        — double-click at pixel (x, y)
- triple_click {x, y}        — triple-click at pixel (x, y)
- type {text}                — type text at the current focus
- scroll {delta}             — vertical scroll by delta pixels (sign = direction)
- hscroll {delta}            — horizontal scroll by delta pixels
- visit_url {url}            — navigate to an http(s) URL
- web_search {query}         — perform a web search for the query
- history_back {}            — go back one page in history
- pause_and_memorize_fact {fact}  — store a fact and pause
- ask_user_question {question}    — ask the user a question and pause
- top_level_memory {description}  — record a memory/observation and continue
- terminate {summary}        — finish the task with a final summary"""

__all__ = [
    "AgentAction",
    "parse_action_arguments",
    "ACTION_SCHEMA",
    "ActionValidationError",
    "ALL_ACTION_NAMES",
    "POINTER_ACTIONS",
    "KEYBOARD_ACTIONS",
    "SCROLL_ACTIONS",
    "NAVIGATION_ACTIONS",
    "CONTROL_ACTIONS",
]