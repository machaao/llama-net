"""Parse Fara1.5-9B model output into structured tool calls.

Fara emits:
  1. A chain-of-thought block (free text, may be XML-tagged as thinking).
  2. A ``<tool_call>...</tool_call>`` block whose body is a JSON object:
     {"name": "computer_use", "arguments": {"action": "...", ...}}.

Quantized GGUF checkpoints frequently emit *broken* JSON — missing brackets,
stray tokens, ``<name`` typed instead of ``{"name``, arguments glued to the
name value. JSON recovery is delegated to the ``json-repair`` library, which
is built exactly for repairing LLM output; this module only extracts the XML
wrapper, applies one trivial pre-normalization, and validates the recovered
object via :mod:`agent.actions`. Unrecoverable output raises
:class:`ToolCallParseError` so the caller can fail closed.
"""

import json
import re
from typing import Any, Dict, Optional, Tuple

from json_repair import loads as repair_json_loads

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
# Unclosed <tool_call> block — the loop passes "</tool_call>" as a stop
# sequence, and llama.cpp consumes it, so the closing tag never appears in
# the raw output even when the model emitted a complete block.
_UNCLOSED_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*)", re.DOTALL | re.IGNORECASE
)


def _normalize_name_slip(text: str) -> str:
    """Fix the ``<name`` slip emitted by quantized models.

    ``<name": "computer_use", ...}`` and ``<name="computer_use", ...}`` are the
    canonical ``{"name": "computer_use", ...}`` with ``<`` typed instead of
    ``{`` (and a missing opening quote on the key). Plain string surgery,
    guarded so genuine ``<name>``-style prose or HTML tags are left untouched.
    """
    idx = text.find("<name")
    if idx == -1:
        return text
    after = idx + len("<name")
    nxt = text[after : after + 1]
    if nxt not in ('"', "=", ":"):
        return text  # not the tool-call slip — leave alone
    repaired = text[:idx] + '{"name' + text[after:]
    # The '<name="x"' form lacks the quote-colon key separator — add it.
    return repaired.replace('{"name="', '{"name":"', 1)


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
        """Return the raw tool-call body, or None when no XML wrapper exists.

        Only the XML wrapper is handled here — closed first, then unclosed
        (llama.cpp consumes the ``</tool_call>`` stop sequence). Fenced/bare
        JSON recovery is delegated to json-repair in :meth:`parse`.
        """
        raw = text or ""
        m = _TOOL_CALL_RE.search(raw)
        if m:
            return m.group(1).strip()
        m = _UNCLOSED_TOOL_CALL_RE.search(raw)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def extract_reasoning(text: str) -> str:
        """Extract the chain-of-thought block (if any) for UI display."""
        raw = text or ""
        m = _THINK_RE.search(raw)
        if m:
            return m.group(1).strip()
        # Fallback: everything before the tool-call block is the CoT.
        tool_match = _TOOL_CALL_RE.search(raw) or _UNCLOSED_TOOL_CALL_RE.search(raw)
        if tool_match:
            return raw[: tool_match.start()].strip()
        # No XML wrapper — CoT is everything before the first JSON brace.
        brace = raw.find("{")
        if brace > 0:
            return raw[:brace].strip()
        return ""

    @classmethod
    def parse(cls, text: str) -> Tuple[str, AgentAction]:
        """Parse model output into (reasoning, AgentAction).

        Raises :class:`ToolCallParseError` when no valid tool call is present.
        """
        raw = _normalize_name_slip(text or "")
        reasoning = cls.extract_reasoning(raw)

        body = cls.extract_tool_call_json(raw)
        if body is None:
            # No XML wrapper at all — scan from the first brace; json-repair
            # natively handles fenced ```json blocks and bare JSON objects.
            brace = raw.find("{")
            if brace == -1:
                raise ToolCallParseError("No valid tool-call JSON found in output")
            body = raw[brace:]

        data: Any = None
        if body.strip():
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # LLM-broken JSON — delegate to json-repair (built for this).
                try:
                    data = repair_json_loads(body)
                except Exception as e:
                    logger.debug(f"json-repair could not recover body: {e}")
                    data = None

        if not isinstance(data, dict) or not data:
            raise ToolCallParseError("No valid tool-call JSON found in output")

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
