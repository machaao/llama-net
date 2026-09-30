"""Parse Fara1.5-9B model output into structured tool calls.

Fara emits:
  1. A chain-of-thought block (free text, may be XML-tagged as thinking).
  2. One or more ``<tool_call>...</tool_call>`` blocks whose body is a JSON
     object: {"name": "computer_use", "arguments": {"action": "...", ...}}.

Quantized GGUF checkpoints frequently emit *broken* JSON — missing brackets,
stray tokens, ``<name`` typed instead of ``{"name``, arguments glued to the
name value, coordinates as arrays, and ``<tool_call>`` repeated as both the
opening and closing tag (with several blocks per turn). JSON recovery is
delegated to the ``json-repair`` library, which is built exactly for repairing
LLM output; this module extracts the XML wrapper(s), applies a few trivial
pre-normalizations, and validates the recovered object via
:mod:`agent.actions`. Unrecoverable output raises :class:`ToolCallParseError`
so the caller can fail closed.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from json_repair import loads as repair_json_loads

from common.utils import get_logger
from agent.actions import AgentAction, parse_action_arguments

logger = get_logger(__name__)


class ToolCallParseError(ValueError):
    """Raised when Fara output cannot be parsed into a valid tool call."""


_THINK_RE = re.compile(
    r"<(?:think|thinking)>\s*(.*?)\s*</(?:think|thinking)>", re.DOTALL | re.IGNORECASE
)
# Models frequently emit `<tool_call>` as BOTH the opening and closing tag
# (repeating the opening tag instead of writing `</tool_call>`), and may emit
# several blocks per turn (e.g. a memory/observation block followed by the
# real action). Split on the opening tag and treat each segment as a candidate.
_TOOL_CALL_SPLIT_RE = re.compile(r"<tool_call>", re.IGNORECASE)
_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call>", re.IGNORECASE)


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


def _normalize_missing_arguments_key(text: str) -> str:
    """Fix ``{"name": "computer_use", {"action": ...}}`` (missing "arguments").

    Quantized models frequently glue the arguments object directly after the
    name value, omitting the ``"arguments":`` key. Insert it so the body is
    valid JSON.
    """
    return re.sub(
        r'("name"\s*:\s*"[^"]*")\s*,\s*\{',
        r'\1, "arguments": {',
        text,
        count=1,
    )


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
    ("description", "top_level_memory"),
    ("summary", "terminate"),
    ("note", "complete_step"),
    ("text", "type"),
    ("delta", "scroll"),
    ("keys", "press_key"),
    ("key", "press_key"),
)


# Quantized models invent near-miss action names ("key_press", "goto",
# "finish"). Map the known near-misses to canonical names before validation;
# unknown names still fail closed.
_ACTION_ALIASES = {
    "key_press": "press_key",
    "presskey": "press_key",
    "press": "press_key",
    "key": "press_key",
    "click": "left_click",
    "goto": "visit_url",
    "navigate": "visit_url",
    "go_back": "history_back",
    "back": "history_back",
    "finish": "terminate",
}


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


def _normalize_coordinates(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize coordinate encodings into separate integer x/y keys.

    Handles the shapes quantized models emit:
      - ``"coordinate": [x, y]`` / ``"coordinates": [x, y]``
      - ``"x": [x, y]`` (the whole pair under the x key)
      - ``"y": [x, y]``
    """
    args = dict(arguments)

    coord = args.get("coordinate") or args.get("coordinates")
    if isinstance(coord, (list, tuple)) and len(coord) == 2:
        args.setdefault("x", coord[0])
        args.setdefault("y", coord[1])

    # The pair may be packed under the x (or y) key as a 2-element array.
    for key in ("x", "y"):
        val = args.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            args["x"] = val[0]
            args["y"] = val[1]
            break

    return args


class ToolCallParser:
    """Extract chain-of-thought and a validated :class:`AgentAction` from text."""

    @staticmethod
    def extract_tool_call_bodies(text: str) -> List[str]:
        """Return every candidate tool-call body found in the output.

        Splits on the opening ``<tool_call>`` tag (which models often repeat in
        place of the closing tag) and trims any trailing ``</tool_call>``. Each
        non-empty segment is a candidate JSON body, in emission order.
        """
        raw = text or ""
        parts = _TOOL_CALL_SPLIT_RE.split(raw)
        bodies: List[str] = []
        for seg in parts[1:]:  # parts[0] is prose before the first tag
            seg = _TOOL_CALL_CLOSE_RE.split(seg, 1)[0].strip()
            if seg:
                bodies.append(seg)
        return bodies

    @staticmethod
    def extract_reasoning(text: str) -> str:
        """Extract the chain-of-thought block (if any) for UI display."""
        raw = text or ""
        m = _THINK_RE.search(raw)
        if m:
            return m.group(1).strip()
        # Fallback: everything before the first tool-call block is the CoT.
        first_tag = _TOOL_CALL_SPLIT_RE.search(raw)
        if first_tag:
            return raw[: first_tag.start()].strip()
        # No XML wrapper — CoT is everything before the first JSON brace.
        brace = raw.find("{")
        if brace > 0:
            return raw[:brace].strip()
        return ""

    @classmethod
    def _parse_body(cls, body: str) -> Optional[Tuple[str, AgentAction]]:
        """Parse a single candidate body into (reasoning, action), or None."""
        body = _normalize_name_slip(body)
        body = _normalize_missing_arguments_key(body)

        # Trim to the first JSON object so trailing prose doesn't confuse repair.
        brace = body.find("{")
        if brace == -1:
            return None
        body = body[brace:]

        data: Any = None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            try:
                data = repair_json_loads(body)
            except Exception as e:
                logger.debug(f"json-repair could not recover body: {e}")
                data = None

        if not isinstance(data, dict) or not data:
            return None

        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            arguments = data

        arguments = _normalize_coordinates(arguments)

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
                arguments = _normalize_coordinates(value)

        if not action_name:
            return None

        action_name = action_name.strip().lower()
        action_name = _ACTION_ALIASES.get(action_name, action_name)

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
            logger.debug(f"Candidate body rejected: Invalid action '{action_name}': {e}")
            return None

        return action_name, AgentAction(name=action_name, arguments=normalized)

    @classmethod
    def parse(cls, text: str) -> Tuple[str, AgentAction]:
        """Parse model output into (reasoning, AgentAction).

        When several tool-call blocks are present (e.g. a memory/observation
        block followed by the real action), the *last* block that validates is
        used — that is the action the model intends to execute this turn.

        Raises :class:`ToolCallParseError` when no valid tool call is present.
        """
        raw = text or ""
        reasoning = cls.extract_reasoning(raw)

        bodies = cls.extract_tool_call_bodies(raw)
        if not bodies:
            # No XML wrapper at all — scan from the first brace; json-repair
            # natively handles fenced ```json blocks and bare JSON objects.
            brace = raw.find("{")
            if brace == -1:
                raise ToolCallParseError("No valid tool-call JSON found in output")
            bodies = [raw[brace:]]

        last_error: Optional[str] = None
        result: Optional[Tuple[str, AgentAction]] = None
        for body in bodies:
            try:
                parsed = cls._parse_body(body)
            except Exception as e:  # defensive — never let one body abort the scan
                last_error = str(e)
                continue
            if parsed is not None:
                result = parsed  # keep the last valid action

        if result is None:
            detail = f": {last_error}" if last_error else ""
            raise ToolCallParseError(f"No valid tool-call JSON found in output{detail}")

        return reasoning, result[1]


__all__ = ["ToolCallParser", "ToolCallParseError"]
