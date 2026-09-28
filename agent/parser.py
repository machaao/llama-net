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

# The JSON body may use several known field layouts.
_ACTION_KEYS = ("action", "action_name", "name", "tool")


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
        raw = text or ""
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

        action_name = None
        for key in _ACTION_KEYS:
            candidate = arguments.get(key)
            if isinstance(candidate, str):
                action_name = candidate
                break

        # Sometimes the action name lives at the top level.
        if action_name is None and isinstance(data.get("name"), str):
            action_name = data.get("name")

        if not action_name:
            raise ToolCallParseError("Tool call missing an action name")

        action_name = action_name.strip().lower()

        # Strip arguments used for dispatch — keep the rest as action args.
        action_args = {k: v for k, v in arguments.items() if k not in _ACTION_KEYS}

        try:
            normalized = parse_action_arguments(action_name, action_args)
        except ValueError as e:
            raise ToolCallParseError(f"Invalid action '{action_name}': {e}")

        return reasoning, AgentAction(name=action_name, arguments=normalized)


__all__ = ["ToolCallParser", "ToolCallParseError"]