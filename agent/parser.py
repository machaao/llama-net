"""Parse Fara1.5-9B model output into structured tool calls.

Fara emits:
  1. A chain-of-thought block (free text, may be XML-tagged as thinking).
  2. A ``<tool_call>...</tool_call>`` block whose body is a JSON object:
     {"name": "computer_use", "arguments": {"action": "...", ...}}.

This module extracts both and validates the tool call via
:mod:`agent.actions`. Any malformed output raises :class:`ToolCallParseError`
so the caller can fail closed.
"""

import json
import re
from typing import Dict, Any, Optional, Tuple

from common.utils import get_logger
from agent.actions import AgentAction, parse_action_arguments

logger = get_logger(__name__)


class ToolCallParseError(ValueError):
    """Raised when Fara output cannot be parsed into a valid tool call."""


_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE
)
_THINK_RE = re.compile(
    r"<(?:think|thinking)>\s*(.*?)\s*</(?:think|thinking)>", re.DOTALL | re.IGNORECASE
)
# Markdown-fenced JSON block, e.g. ```json { ... } ``` — quantized models
# sometimes strip the XML wrapper and emit a fenced JSON object instead.
_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)
# Bare JSON object anywhere in the text (last resort).
_BARE_JSON_RE = re.compile(r"\{[^{}]*\"(?:name|action)\"[^{}]*\}", re.DOTALL | re.IGNORECASE)
# Unclosed <tool_call> block — the loop passes "</tool_call>" as a stop
# sequence, and llama.cpp consumes it, so the closing tag never appears in
# the raw output even when the model emitted a complete block.
_UNCLOSED_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*)", re.DOTALL | re.IGNORECASE
)


# Known malformations emitted by quantized models (observed in real runs):
#   1. {"name": "computer_use", {"action": ...}}  — arguments object glued to
#      the name value with no "arguments" key.
#   2. <name="computer_use", ...}                 — "<" instead of "{" and
#      name= instead of "name":.
#   3. <name": "computer_use", ...}               — same as 2 but with the
#      quote-colon form of the key separator.
_JSON_REPAIRS: tuple = (
    (
        re.compile(r'\{\s*"name"\s*:\s*("[^"]+")\s*,\s*\{'),
        r'{"name": \1, "arguments": {',
    ),
    (
        # Covers both <name="computer_use" and <name": "computer_use" —
        # anything between "name" and the first quoted value is separator junk.
        re.compile(r'<\s*name[^"]*("[^"]+")'),
        r'{"name": \1',
    ),
)


def _repair_tool_call_text(text: str) -> str:
    """Repair known quantized-model tool-call malformations in raw output.

    Each pattern only matches a malformed shape — never the canonical
    format — so applying the repairs unconditionally is safe.
    """
    repaired = text or ""
    for pattern, replacement in _JSON_REPAIRS:
        repaired = pattern.sub(replacement, repaired)
    return repaired


def _extract_balanced_json(text: str) -> Optional[str]:
    """Extract the first balanced ``{...}`` object from text.

    String-aware brace-depth scan — tolerant of trailing prose or special
    tokens that quantized models append after the JSON body.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None

# The JSON body may use several known field layouts.
_ACTION_KEYS = ("action", "action_name", "name", "tool")

# Argument-shape → canonical action mapping. Used when the model emits the
# ``computer_use`` *wrapper* name without an inner ``action`` key — quantized
# models frequently do this. Order matters: most specific first.
_INFER_FROM_ARGS: tuple = (
    ("url", "visit_url"),
    ("query", "web_search"),
    ("question", "ask_user_question"),
    ("fact", "pause_and_memorize_fact"),
    ("summary", "terminate"),
    ("text", "type"),
    ("delta", "scroll"),
)


def _infer_action(arguments: Any) -> Optional[str]:
    """Infer the canonical action name from a bare argument dict.

    Returns None when the arguments are empty or ambiguous (caller fails
    closed in that case).
    """
    if not isinstance(arguments, dict):
        return None
    for key, action in _INFER_FROM_ARGS:
        if key in arguments:
            return action
    if "x" in arguments and "y" in arguments:
        return "left_click"
    return None


class ToolCallParser:
    """Extract chain-of-thought and a validated :class:`AgentAction` from text."""

    @staticmethod
    def extract_tool_call_json(text: str) -> Optional[str]:
        """Return the inner JSON string of the tool-call, trying multiple formats.

        Order of precedence (most explicit → most lenient):
          1. ``<tool_call>...</tool_call>`` XML block (canonical Fara format)
          2. Markdown-fenced JSON: ```` ```json { ... } ``` ````
          3. Bare JSON object containing an ``action``/``name`` key
        """
        raw = text or ""
        m = _TOOL_CALL_RE.search(raw)
        if m:
            return m.group(1).strip()

        # Unclosed <tool_call> block: the loop stops on "</tool_call>", llama.cpp
        # consumes the stop sequence, so the closing tag is missing even when
        # the model emitted a complete block. Fall back to a balanced-JSON
        # scan of everything after the opening tag.
        m = _UNCLOSED_TOOL_CALL_RE.search(raw)
        if m:
            balanced = _extract_balanced_json(m.group(1))
            if balanced:
                return balanced.strip()

        m = _FENCED_JSON_RE.search(raw)
        if m:
            return m.group(1).strip()

        m = _BARE_JSON_RE.search(raw)
        if m:
            return m.group(0).strip()

        return None

    @staticmethod
    def extract_reasoning(text: str) -> str:
        """Extract the chain-of-thought block (if any) for UI display."""
        raw = text or ""
        m = _THINK_RE.search(raw)
        if m:
            return m.group(1).strip()
        # Fallback: everything before the tool_call/JSON block is the CoT.
        tool_match = _TOOL_CALL_RE.search(raw)
        if not tool_match:
            tool_match = _FENCED_JSON_RE.search(raw)
        if not tool_match:
            tool_match = _BARE_JSON_RE.search(raw)
        if tool_match:
            preceding = raw[: tool_match.start()]
            return preceding.strip()
        return ""

    @classmethod
    def parse(cls, text: str) -> Tuple[str, AgentAction]:
        """Parse model output into (reasoning, AgentAction).

        Raises :class:`ToolCallParseError` when no valid tool call is present.
        """
        # Repair known quantized-model malformations before extraction
        # ({"name": "computer_use", {...}} and <name="computer_use", ...}).
        raw = _repair_tool_call_text(text or "")
        reasoning = cls.extract_reasoning(raw)

        json_str = cls.extract_tool_call_json(raw)
        if not json_str:
            raise ToolCallParseError("No <tool_call> block found in output")

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ToolCallParseError(f"<tool_call> body is not valid JSON: {e}")

        if not isinstance(data, dict):
            raise ToolCallParseError("<tool_call> body must be a JSON object")

        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            arguments = data

        # Some quantized models emit "coordinate": [x, y] (array) instead of
        # separate x/y keys — normalize before schema validation.
        coord = arguments.get("coordinate") or arguments.get("coordinates")
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            arguments = dict(arguments)
            arguments.setdefault("x", coord[0])
            arguments.setdefault("y", coord[1])

        action_name = None
        for key in _ACTION_KEYS:
            candidate = arguments.get(key)
            if isinstance(candidate, str):
                action_name = candidate
                break

        # Sometimes the action name lives at the top level.
        if action_name is None and isinstance(data.get("name"), str):
            action_name = data.get("name")

        # Alternate serialization: {"visit_url": {"url": ...}} — a single-key
        # object mapping action_name → arguments (common Qwen/Fara quantized
        # format). Resolve it before failing on a missing action name.
        if action_name is None and len(data) == 1:
            only_key = next(iter(data))
            value = data[only_key]
            if only_key not in _ACTION_KEYS and isinstance(value, dict):
                logger.debug(
                    f"Resolved alternate tool-call serialization: "
                    f"{{'{only_key}': {{...}}}} → action '{only_key}'"
                )
                action_name = only_key
                arguments = value

        if not action_name:
            raise ToolCallParseError("Tool call missing an action name")

        action_name = action_name.strip().lower()

        # ``computer_use`` is the tool *wrapper* name, not an action. Quantized
        # models often omit the inner ``action`` key — infer the real action
        # from the argument shape instead of failing on the wrapper name.
        if action_name in ("computer_use", "computer-use", "computer use"):
            inferred = _infer_action(arguments)
            if inferred:
                logger.debug(
                    f"Inferred action '{inferred}' from arguments "
                    f"(wrapper name 'computer_use')"
                )
                action_name = inferred

        # Strip arguments used for dispatch — keep the rest as action args.
        action_args = {k: v for k, v in arguments.items() if k not in _ACTION_KEYS}

        try:
            normalized = parse_action_arguments(action_name, action_args)
        except ValueError as e:
            raise ToolCallParseError(f"Invalid action '{action_name}': {e}")

        return reasoning, AgentAction(name=action_name, arguments=normalized)


__all__ = ["ToolCallParser", "ToolCallParseError"]