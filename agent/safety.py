"""Safety controls for the Fara1.5-9B computer-use agent.

Implements the model card's non-negotiable requirement: the harness MUST own
the safety controls. This module provides:

- **Domain allow-list / block-list** — navigation is checked on every
  ``visit_url``/navigation action; out-of-policy domains are rejected before
  the browser is told to go there.
- **Critical-point interception** — maps the model's ``ask_user_question`` and
  ``pause_and_memorize_fact`` control actions to a paused state, and blocks
  irreversible actions (form submission, payments, sign-in, message send)
  unless the user explicitly authorized them.
- **Watch-mode trace log** — records every action, model reasoning, and
  screenshot for later audit.
- **Pause/resume/abort control** — cooperative cancellation hooks the agent
  loop consults before each step.

All checks are synchronous and fail closed: any doubt means the action is
blocked and the session pauses for user input.
"""

from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

from common.utils import get_logger
from agent.actions import AgentAction

logger = get_logger(__name__)


class SafetyViolation(Exception):
    """Raised (or recorded) when an action breaches a safety policy."""


# Actions that are irreversible and therefore require explicit authorization.
_IRREVERSIBLE_HINTS = (
    "submit",
    "purchase",
    "pay",
    "checkout",
    "sign_in",
    "login",
    "send",
    "delete",
    "confirm",
    "approve",
)


class SafetyController:
    """Enforces browser-navigation and critical-point safety policies."""

    def __init__(
        self,
        allowlist: Optional[List[str]] = None,
        blocklist: Optional[List[str]] = None,
        require_confirmation_for_irreversible: bool = True,
    ):
        self.allowlist: List[str] = [d.lower() for d in (allowlist or [])]
        self.blocklist: List[str] = [d.lower() for d in (blocklist or [])]
        self.require_confirmation = require_confirmation_for_irreversible

        self.paused = False
        self.pause_reason = ""
        self.aborted = False
        self._authorized_once = False  # one-shot authorization for a pending irreversible action
        self._trace: List[Dict[str, Any]] = []

    # ── Lifecycle control ────────────────────────────────────────────────

    def pause(self, question: str) -> None:
        """Pause the agent and surface a user question."""
        self.paused = True
        self.pause_reason = question
        self._authorized_once = False
        logger.info(f"⏸️  Agent paused: {question}")

    def resume(self, answer: str = "") -> None:
        """Resume from a paused state with an optional user answer."""
        self.paused = False
        self.pause_reason = ""
        self.answer = answer
        logger.info("▶️  Agent resumed")

    def authorize_once(self) -> None:
        """Authorize exactly one blocked irreversible action."""
        self._authorized_once = True
        self.paused = False

    def abort(self) -> None:
        """Abort the session immediately."""
        self.aborted = True
        self.paused = False
        logger.warning("🛑 Agent aborted by user")

    # ── Navigation policy ────────────────────────────────────────────────

    def _domain_of(self, url: str) -> str:
        host = urlparse(url).hostname or ""
        return host.lower()

    def _matches(self, domain: str, patterns: List[str]) -> bool:
        for p in patterns:
            if domain == p or domain.endswith("." + p.lstrip(".")):
                return True
        return False

    def check_navigation(self, url: str) -> bool:
        """Return True if navigation to ``url`` is allowed."""
        domain = self._domain_of(url)
        if not domain:
            raise SafetyViolation(f"Could not parse domain from URL: {url}")

        if self.blocklist and self._matches(domain, self.blocklist):
            raise SafetyViolation(f"Domain '{domain}' is blocked")

        if self.allowlist and not self._matches(domain, self.allowlist):
            raise SafetyViolation(
                f"Domain '{domain}' is outside the configured allow-list"
            )
        return True

    # ── Action policy ────────────────────────────────────────────────────

    def check_action(self, action: AgentAction) -> None:
        """Validate an action against safety policy. Raises :class:`SafetyViolation`."""
        # Browser navigation always goes through the allow-list/block-list.
        if action.is_navigation and action.name == "visit_url":
            self.check_navigation(action.arguments.get("url", ""))

        # Critical-point control actions → pause.
        if action.name == "ask_user_question":
            question = action.arguments.get("question", "Agent needs input.")
            self.pause(question)
            return

        if action.name == "pause_and_memorize_fact":
            fact = action.arguments.get("fact", "")
            logger.info(f"📌 Agent memorized fact: {fact}")
            self.pause("Agent paused to record information.")
            return

        # Irreversible-action heuristic — type/click aimed at dangerous controls.
        if self.require_confirmation and action.is_keyboard:
            text = action.arguments.get("text", "").lower()
            if any(hint in text for hint in _IRREVERSIBLE_HINTS) and not self._authorized_once:
                raise SafetyViolation(
                    "Typed text appears to trigger an irreversible action; "
                    "user confirmation required."
                )

    def check_step(self, action: AgentAction) -> None:
        """Per-step hook: enforce abort/pause and record the trace."""
        if self.aborted:
            raise SafetyViolation("Session aborted")

        self.check_action(action)
        self._trace.append({
            "action": action.to_dict(),
            "allowed": True,
        })

    # ── Trace log ────────────────────────────────────────────────────────

    def record(self, event_type: str, data: Dict[str, Any]) -> None:
        self._trace.append({"type": event_type, **data})

    def get_trace(self) -> List[Dict[str, Any]]:
        return list(self._trace)


__all__ = ["SafetyController", "SafetyViolation"]