"""Fara1.5-9B computer-use agent subsystem for LlamaNet.

Drives a headless browser, feeds screenshots to the locally loaded Fara model
through LlamaWrapper, parses the XML tool-call output, executes browser
actions, and enforces the safety controls required by the model card.
"""

from agent.actions import (
    AgentAction,
    parse_action_arguments,
    ACTION_SCHEMA,
)
from agent.parser import ToolCallParser, ToolCallParseError
from agent.safety import SafetyController, SafetyViolation
from agent.loop import AgentLoop, AgentState

__all__ = [
    "AgentAction",
    "parse_action_arguments",
    "ACTION_SCHEMA",
    "ToolCallParser",
    "ToolCallParseError",
    "SafetyController",
    "SafetyViolation",
    "AgentLoop",
    "AgentState",
]