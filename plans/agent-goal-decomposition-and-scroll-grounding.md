# Plan: Goal Decomposition + Scroll Grounding for the Agent Loop

Reference session: goal *"goto machai.live and scroll the page from top to bottom"*
ended with the agent oscillating up/down and the session stopped as "confused".

## Diagnosis (from the session log)

Two independent problems caused the oscillation:

1. **No plan tracking.** The goal lives only as one string; each step the model
   re-derives "what now?" from the screenshot, so it loses its place in a
   multi-phase task (navigate → scroll down → verify bottom → finish).
2. **Ambiguous scroll feedback.** The model invented an inverted sign convention
   (used `-740` for "down", which is *up* in Playwright's `mouse.wheel`), and the
   loop only reports scroll results on *failure*. It never told the model: current
   `scrollY`, max scroll, "you are at the bottom", or "positive delta = down".
   Hence the endless down/up probing.

## Changes

### 1. `agent/browser_session.py` — expose authoritative scroll state

- Add `_scroll_state()` — one `page.evaluate` returning
  `{x, y, max, at_bottom, at_top, can_scroll}`
  (`max = scrollHeight - clientHeight`, `at_bottom = y >= max - 2`,
  `at_top = y <= 2`, `can_scroll = max > 10`).
- Add `current_scroll()` — safe wrapper returning
  `{x, y, max_y, at_bottom, at_top, can_scroll}` (empty defaults when no page).
- Extend `scroll_by()` to use `_scroll_state()` for before/after and add
  `scroll_y_before`, `max_scroll_y`, `at_bottom`, `at_top` to its result
  (existing keys `scrolled`, `scroll_x`, `scroll_y`, `can_scroll`,
  `delta_x`, `delta_y` kept — the loop already reads them).

### 2. `agent/actions.py` — add a plan-advance control action

- Add `"complete_step"` to `CONTROL_ACTIONS`.
- Validation branch: `complete_step` accepts an optional string `note`
  (default `""`), returns `{"note": ...}`.
- Append to `ACTION_SCHEMA`:
  `- complete_step {note} — mark the current plan step accomplished (no browser action) and continue`.

### 3. `agent/parser.py` — wrapper-name inference for the new action

- In `_INFER_FROM_ARGS`, add `("note", "complete_step")` immediately after
  `("summary", "terminate")`, so a bare `computer_use` wrapper carrying only a
  `note` resolves correctly.

### 4. `agent/prompts.py` — plan usage instructions

- New exported constant `PLAN_INSTRUCTIONS`: work plan steps in order; emit
  `complete_step {note}` once the current step is fully achieved; emit
  `terminate {summary}` only when every plan step is done; scroll convention —
  **positive `delta` scrolls down (increases scrollY), negative scrolls up**.

### 5. `agent/loop.py` — the bulk of the work

- **State:** `self.plan: List[str] = [goal]`, `self.plan_index: int = 0`.
- **`_create_plan()`:** one text-only call via `generate_chat_safe`
  (max_tokens 400, temperature 0.2) asking for a 2–6 step JSON array; parse by
  slicing first `[` … last `]` through `json_repair.loads`; on any failure fall
  back to `[goal]` (plan is advisory grounding, not a safety gate). Append
  `PLAN_INSTRUCTIONS` + numbered plan to `self.system_prompt`; emit
  `plan_created {steps}`.
- **`start()`:** call `_create_plan()` after the browser settles, before
  `session_started`.
- **`_build_messages(..., scroll=None)`:**
  - Prepend a plan block to the user turn:
    `Goal: … / Plan: 1. [done] … 2. [CURRENT] … 3. [ ] … /
    Current plan step: N of M — …`
  - Append scroll grounding to `page_context`:
    `Scroll position: y=… of … px (…% from top; positive delta scrolls down)`
    plus `Page is at the BOTTOM — no more content below.` / `… at the TOP.`
- **`step()`:**
  - Fetch `scroll = await browser.current_scroll()` alongside url/title;
    include `scroll_y` / `max_scroll_y` in the `step_screenshot` event; pass
    `scroll` into `_build_messages`.
  - Handle `complete_step` (after the `terminate` block, before execution):
    append assistant turn, advance `plan_index` (clamped), increment
    `steps_completed` (counts toward `max_steps` so spam can't loop forever),
    emit `plan_step_completed {index, completed_step, note, remaining}`, honor
    the step limit, return — no browser action.
  - Scroll feedback on **success** (currently only failures get feedback):
    always build `action_feedback` for `action.is_scroll` when `scrolled`:
    `Scrolled DOWN|UP by Npx: scrollY A → B (max Cpx). Positive delta scrolls DOWN.`
    + `Page is at the BOTTOM — no more content below; the scroll goal is achieved.`
    when `at_bottom`. Failure branches reworded to state the convention
    (positive = down) instead of "sign may be inverted — try the opposite".
- **`get_state()`:** add `plan` and `plan_index`.

### 6. `static/agent.js` — render the plan

- `plan_created` → `system` log entry with numbered steps.
- `plan_step_completed` → `action` log entry `Plan step N complete` with note.
- `step_screenshot` → append ` · scroll N%` to the step body when
  `max_scroll_y > 0`.

No `agent.html` change required — the plan renders in the existing step log.

## Why this fixes the observed failure

- The model sees "you are on step 2 of 3: scroll to bottom" every turn instead
  of re-deriving intent → no goal drift.
- Every scroll returns the decisive facts (position, max, bottom-reached, sign
  convention) → the up/down probing loop collapses into: scroll down →
  `at_bottom` → `complete_step` → `terminate`.

## Verification

1. Re-run *"goto machai.live and scroll the page from top to bottom"*: expect
   `plan_created` (navigate / scroll down / verify bottom), monotonic `scrollY`
   growth with no positive-delta "probe up" steps, an explicit bottom-reached
   feedback line, `complete_step`, then `terminate` — well under 30 steps.
2. A goal with no decomposition need (e.g. *"open google.com"*) still produces a
   sane 1–2 step plan and completes.
3. Force a malformed plan response (dev run) and confirm the session still runs
   with the goal as the single plan step.
4. `GET /agent/sessions/{id}` returns `plan` + `plan_index`.

## Out of Scope

- No changes to `agent/safety.py`, the Fara system prompt text, or the action
  executor beyond the new control action.
- No harness switch to MagenticLite/Fara CLI.
