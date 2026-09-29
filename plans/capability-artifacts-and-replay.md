# Plan: Capability Artifacts, Deterministic Replay & Control Handoff

Turn a successful agent run into a **reusable, versioned capability artifact** that
can be re-executed deterministically (no model in the loop), with typed inputs,
typed outputs, explicit error/outcome handling, and a real human handoff seam.

Today a run ends with `SafetyController.get_trace()` — an audit trail, not an
invocable capability. Every run is LLM-driven from scratch, and the
pause/answer flow relays text only (the operator cannot drive the live session).

## Design principles

1. **The model discovers; the artifact is the product.** The recorder mines a
   completed `AgentLoop` trajectory into a typed artifact; replay never calls
   the model.
2. **Three-way result contract.** Business outcomes ("member not found") are
   legitimate results — never conflated with recoverable conditions or hard
   failures.
3. **Surface seam.** Perception/actuation (`BrowserSession`) stays behind a
   small protocol so legacy/desktop surfaces can plug in without touching the
   recorded flow.
4. **Fail closed.** Locator chains resolve with verification; anything
   ambiguous stops with evidence rather than guessing.

## New modules (`agent/`)

| File | Responsibility |
|---|---|
| `agent/locators.py` | `LocatorCandidate` (strategy + matcher + verification) and `Locator` = ordered fallback chain. Strategies: `test_id` → `role_text` → `element_id` → `css` → `text_match` → `point_signature` (coordinates + element tag/text captured at record time). `resolve(surface) -> element \| None` walks the chain in order. Each candidate carries a `rationale` string for reviewability. |
| `agent/artifact.py` | `CapabilityArtifact` (pydantic model, `schema_version: "1.0"`): `id`, `name`, `description`, `version`, `status: draft \| approved`, `app` (`surface_kind`, `entry_url`), `inputs: [Param{name, type, required, validation}]`, `outputs: [Output{name, type, extraction}]`, `steps: [Step{index, intent, action, target: Locator, params_used, postcondition, on_recoverable: RetryPolicy, on_business_outcome}]`, `checkpoint: Assertion`, `business_outcomes: [Outcome{name, detected_by, returns}]`, `provenance` (recorded run id, model, timestamp, evidence ref), `tenant: {base_app, overrides}`. Serializable to JSON; `to_dict()`/`from_dict()` + `validate()`. Reuses `agent/actions.py` action names as the step vocabulary. |
| `agent/recorder.py` | `record(loop: AgentLoop, ...) -> CapabilityArtifact`. Consumes `safety.get_trace()` + per-step grounding (see loop change below). Parameterization: literal values matching a declared input name (e.g. values typed into forms / IDs in URLs like `/member/{{member_id}}`) become `{{param}}` bindings; outputs are extracted from text captured near the checkpoint (page text at terminate); checkpoint is derived from the `terminate` step + final URL/title assertion. |
| `agent/replay.py` | `ReplayEngine(artifact, inputs).run(browser) -> ReplayResult`. Per step: bind params → `Locator.resolve()` → execute via the same `execute_action()` used by discovery → verify `postcondition` → apply `on_recoverable` policy (bounded retries/wait/dismiss) or classify `on_business_outcome` → extract outputs → verify `checkpoint`. **No LLM imports.** |
| `agent/errors.py` | Error taxonomy: `BusinessOutcome(name, data)`, `RecoverableCondition(kind, detail)` with `RetryPolicy{max_retries, wait_ms, on_exhausted}`, `HardFailure(step_index, intent, expected, observed, evidence_refs)`. `ReplayResult.status: success \| business_outcome \| recoverable_exhausted \| failure` with `outputs`, `outcome`, or debug detail respectively. |
| `agent/escalation.py` | `ControlState` machine: `AUTOMATION → ESCALATED → HUMAN_CONTROL → RETURNING → AUTOMATION`. `InterventionRequest{session_id, goal/capability, step_index, reason, screenshot_ref, recent_actions}`. Human actions are executed through the same `execute_action()` path and appended to history as `"role": "human"` turns, so the record is uniform. |
| `agent/redaction.py` | `redact(text) -> str`: SSN, card numbers, account/routing numbers, password/secret-shaped typed values. Applied in `SafetyController.record()`, artifact serialization, and evidence export. |
| `agent/evidence.py` | `write_run_evidence(run_id, ...)` → `/evidence/<run-id>/`: `trace.jsonl`, screenshots, `artifact.json` (discovery), `replay-result.json`, failure-time `dom.html` (via `BrowserSession.page.content()`). |
| `agent/surface.py` | `Surface` protocol: `perceive() -> SurfaceState`, `act(action) -> ActionResult`. `PlaywrightSurface` wraps `BrowserSession`. `DesktopSurface` stub documenting the a11y-tree extension point. |
| `agent/cli.py` | `python -m agent.cli discover --goal ... --record artifact.json` and `python -m agent.cli replay --artifact artifact.json --inputs inputs.json` — the documented demo path. |

## Modified files

| File | Changes |
|---|---|
| `agent/loop.py` | (a) `step()`: after `element_at()`/click verification, attach `grounding` (element tag/id/class/text + URL + coordinates) to the corresponding trace entry — raw material for locator chains. (b) **Escalation on stuck**: when `MAX_IDENTICAL_REPEATS` or `MAX_PARSE_RETRIES` is exhausted, raise `InterventionRequest` (state `WAITING_FOR_USER` with `reason: "stuck"`) instead of only warning/failing. (c) `resume()` accepts a list of human actions recorded during handoff and appends them to `history`. No change to observe→decide→act core. |
| `agent/server.py` | New endpoints: `GET /agent/artifacts`, `GET /agent/artifacts/{id}`, `POST /agent/artifacts/{id}/replay` (runs `ReplayEngine` in a background task, streams/returns `ReplayResult`), `POST /agent/sessions/{id}/take-control` (AUTOMATION→HUMAN_CONTROL; loop task suspends — not cancels — browser stays open), `POST /agent/sessions/{id}/human-actions` (execute + record operator actions), `POST /agent/sessions/{id}/hand-back` (resume loop with human turns appended). Existing endpoints unchanged. |
| `agent/safety.py` | Add optional `allowed_actions: Set[str]` (action-type allowlist; anything outside → `SafetyViolation`). Route all `record()` payloads through `agent/redaction.py`. |
| `static/agent.html` + `static/agent.js` + `static/agent.css` | Minimal operator surface: control-owner badge ("Automation" / "Human"), **Take Control** button (enabled on `waiting_for_user`/stuck), click-coordinate + type form driving `human-actions`, **Hand Back** button. Human actions render in the step log tagged `human`. |
| `README.md` | New "Capability recording & replay" section with the two demo commands (discover → artifact → replay), in the existing product-docs voice. |
| `docs/agent.md`, `docs/api-reference.md` | Document the new endpoints and CLI briefly — same voice, no internal schema dumps. |

## Replay result contract (the load-bearing decision)

```
success            → checkpoint verified; outputs populated
business_outcome   → declared outcome detected (e.g. "member_not_found"); NOT an error
recoverable_exhausted → retry/wait/dismiss policies spent at step N; includes what was tried
failure            → hard failure: {step_index, step_intent, expected, observed, evidence_refs}
```

## Escalation & handoff model

- **Stuck detection**: repeated-action threshold, parse-retry exhaustion,
  postcondition verification failure N times → `InterventionRequest` with full
  context (goal/capability, step, screenshot, recent actions, reason).
- **The seam**: the loop's background task *suspends* (browser session stays
  open); `ControlState` tracks the owner. Human drives via `human-actions`
  (same executor path as automation), then `hand-back` resumes with human
  turns in history. Every transition is recorded in the trace (who, when, what).
- Operator UI is deliberately minimal (mock-grade is acceptable) — the
  control-transfer mechanism is the real deliverable.

## Heterogeneity & multi-tenant (design anchors, not implementations)

- `agent/surface.py` is the perceive/act seam; `LocatorCandidate` strategies
  show how a frameset/legacy web or desktop a11y-tree surface slots in without
  changing the recorded flow.
- `tenant.overrides` in the schema + canonicalized patterns
  (`/member/{{member_id}}`) is the cross-tenant reuse story; one override
  example in `/evidence/` demonstrates it cheaply.

## Sequencing

1. `locators.py` + `artifact.py` + `recorder.py` (schema first — it shapes everything)
2. `errors.py` + `replay.py` + `agent/cli.py` replay path
3. `redaction.py` + safety action allowlist + loop grounding capture
4. `escalation.py` + server endpoints + minimal operator UI
5. `evidence.py` + real discovery run + replay runs (success + one error case) + README/docs

Steps 1–2 are the core; 4 is the handoff seam; 5 is the demo surface.

## Verification

1. **Discover**: `python -m agent.cli discover --goal "open example.com and read the heading" --record artifact.json` → artifact with ≥1 parameterized input, checkpoint, and locator chain with rationale.
2. **Replay**: `python -m agent.cli replay --artifact artifact.json --inputs inputs.json` → `status: success` with outputs, zero LLM calls (assert no model traffic).
3. **Error case**: replay with a bad input (nonexistent id) → `business_outcome` or `failure` with step/expected/observed — not a crash. Saved under `/evidence/`.
4. **Handoff**: run a goal with an irreversible action → `waiting_for_user` → `take-control` → drive 1–2 human actions → `hand-back` → run resumes and completes; trace shows control transitions and human turns.
5. **Redaction**: type a fake SSN/card during a run → trace, artifact, and evidence contain the redacted form only.

## Out of scope (document as cuts)

- Real-time co-browsing operator console (minimal handoff surface instead).
- Desktop surface implementation (`DesktopSurface` stub + design story only).
- Confidence scoring / draft→approved gating (stretch, not core).
- Cross-tenant re-record tooling beyond one override example.
- Queues/cluster/multi-tenant infrastructure — schema-level support only.
