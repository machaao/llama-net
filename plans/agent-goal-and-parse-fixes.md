# Plan: Fix Agent Goal Visibility, Tool-Call Parsing, and Step Log

Reference: [microsoft/Fara1.5-9B model card](https://huggingface.co/microsoft/Fara1.5-9B)
(inputs: user goal (text), current screenshot(s), prior thoughts/actions; output:
chain-of-thought block followed by an XML-tagged tool-call block).

## Problem (observed in agent console)

Given the goal *"open the browser, goto google.com and search for latest ai news"*,
the model reasoned about a completely different task (JFK→LHR flights on
united.com — a Fara training example) and the run failed with:

1. `Invalid action 'computer_use': Unknown action 'computer_use'`
2. `No <tool_call> block found in output` (after the parse retry)
3. Step log jumps from "Browser session started" straight to "Retrying parse" —
   no goal, no step markers ("out of whack").

## Root Causes

| # | Root cause | Location |
|---|---|---|
| 1 | **Goal not salient to the model.** The goal appears only in the final user turn (`_build_messages` → `f"Goal: {self.goal}\nWhat is the next action?"`). The system prompt is the verbatim Fara prompt with no goal, so a quantized 9B model drifts to its training example. | `agent/loop.py`, `agent/prompts.py` |
| 2 | **Parser treats the tool *wrapper* name as the action.** The model emits `{"name": "computer_use", "arguments": {"url": ...}}` with no `action` key. `parse()` falls back to `data["name"]` = `computer_use`, which is not in `ALL_ACTION_NAMES` → `Unknown action 'computer_use'`. | `agent/parser.py` |
| 3 | **Corrective nudge is circular.** The retry message tells the model to emit `{"name": "computer_use", "arguments": {...}}` — the exact shape the parser rejects — and never says `arguments.action` is required. | `agent/loop.py` |
| 4 | **Step log has no goal or step markers.** `session_started` carries `goal` but the UI ignores it; `step_screenshot` updates the image without a log entry. | `static/agent.js` |

## Changes

### 1. Make the goal prominent in the prompt — `agent/prompts.py`, `agent/loop.py`

- `build_system_prompt(allowlist, goal="")` gains a `goal` parameter. When set,
  append a dedicated section before `ACTION_SCHEMA`, e.g.
  `Current task goal (this is the ONLY task you are working on): {goal}`.
- `AgentLoop.__init__` passes `goal=goal` to `build_system_prompt`.
- Keep the per-step user turn (`Goal: ...`) as-is — repetition is intentional
  for a 9B model.

### 2. Infer the action from arguments for the `computer_use` wrapper — `agent/parser.py`

- Add helper `_infer_action(arguments) -> Optional[str]` mapping argument shape
  to the canonical action:
  - `url` → `visit_url`, `query` → `web_search`, `question` → `ask_user_question`,
    `fact` → `pause_and_memorize_fact`, `summary` → `terminate`, `text` → `type`,
    `delta` → `scroll`, `x`+`y` → `left_click`.
- In `parse()`, after resolving `action_name`: if it is the wrapper name
  (`computer_use` / `computer-use`), replace it with `_infer_action(arguments)`.
  If inference returns `None` (empty/ambiguous args), keep the current
  fail-closed `ToolCallParseError`.
- Log the inference at debug level for diagnosability.

### 3. Fix the corrective nudge — `agent/loop.py`

- Rewrite the parse-retry user message to state explicitly that the JSON body
  must include an `action` field inside `arguments`, with one correct example,
  e.g. `{"name": "computer_use", "arguments": {"action": "visit_url", "url": "..."}}`.
- This removes the circular reinforcement of the rejected shape.

### 4. Make the step log readable — `static/agent.js`

- On `session_started`: append a `system` log entry including `data.goal`
  (the event already carries it) so the operator can verify the goal reached
  the agent.
- On `step_screenshot`: append a compact step marker entry
  (`Step ${data.index}`) so thinking/action entries are ordered per step.
- No other UI changes.

## Out of Scope

- No changes to the Fara system prompt text (must stay verbatim per model card).
- No changes to `agent/actions.py` schema, `agent/safety.py`, or the browser
  session.
- No harness switch to MagenticLite/Fara CLI (native harness remains the
  design per `plans/fara-cua-support.md`).

## Verification

1. Start a session with goal *"open the browser, goto google.com and search for
   latest ai news"* and no start URL.
2. Expect: first step's reasoning references the actual goal (not a training
   example); the tool call parses on the first attempt (`visit_url`/`web_search`).
3. Force a malformed output (e.g. temporarily break the regex in a dev run) and
   confirm the retry nudge produces a parseable `arguments.action` on attempt 2.
4. Confirm the step log shows: session created (with goal) → Step 1 marker →
   thinking → action → … in order.