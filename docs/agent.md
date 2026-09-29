# Computer-Use Agent (Fara1.5-9B)

[← Back to README](../README.md)

LlamaNet includes a **native computer-use agent harness** for [`microsoft/Fara1.5-9B`](https://huggingface.co/microsoft/Fara1.5-9B) (GGUF: [`bartowski/Fara1.5-9B-GGUF`](https://huggingface.co/bartowski/Fara1.5-9B-GGUF)).

Fara is a vision-language model that observes the browser through screenshots and emits structured tool calls (click, type, scroll, visit URL, web search, …) to complete tasks end-to-end. The harness runs a headless browser via Playwright, feeds screenshots to the model, parses tool calls, executes actions, and enforces the model's critical-point safety design.

## Requirements

1. **Fara model + vision projector** — download `bartowski/Fara1.5-9B-GGUF` (any quantization; `Q4_K_M` ≈ 5.91 GB) via the Model Manager. The `mmproj-Fara1.5-9B.gguf` vision projector (~500 MB) downloads automatically with the model; it can also be fetched manually from the same repo or from `microsoft/Fara1.5-9B`.
2. **Playwright + Chromium** — required for browser automation. `start-app.sh` and the Docker image install them automatically. To install manually:

```bash
pip install playwright
playwright install chromium
```

> **Note:** `start-app.bat` (Windows) does not install Playwright automatically. Run the two commands above first if you want the agent console on Windows.

## Usage

1. Download and load `bartowski/Fara1.5-9B-GGUF` (any quantization; `Q4_K_M` ≈ 5.91 GB) via the Model Manager.
2. Open the **Agent** console (`/agent` in the Web UI, or click **Agent** in the navbar).
3. Enter a goal (optionally a start URL and domain allow-list), then **Run**.

## Using the Agent as an API (custom apps)

The agent is a plain REST + SSE API on the inference node — any HTTP client can drive it, no browser UI required. See the [API Reference](api-reference.md) for the full endpoint list.

**Minimal Python client:**

```python
import json
import requests
import sseclient  # pip install sseclient-py

BASE = "http://localhost:8000"  # or the node's tunnel URL

# 1. Create the session
r = requests.post(f"{BASE}/agent/sessions", json={
    "goal": "goto google.com and search for latest ai news",
    "allowlist": ["google.com"],   # optional domain restriction
    "headless": True,
    "max_steps": 30,
})
session_id = r.json()["session_id"]

# 2. Start the loop (returns immediately — runs in the background)
requests.post(f"{BASE}/agent/sessions/{session_id}/run")

# 3. Consume the SSE event stream
resp = requests.get(f"{BASE}/agent/sessions/{session_id}/events", stream=True)
for event in sseclient.SSEClient(resp):
    data = json.loads(event.data)
    t = data.get("type")
    if t == "step_screenshot":
        print(f"Step {data['index']}: {data.get('url')}")
    elif t == "thinking":
        print("CoT:", data.get("content", "")[:200])
    elif t == "waiting_for_user":
        # Critical point — answer to continue
        ans = input(f"Agent asks: {data.get('question')} > ")
        requests.post(f"{BASE}/agent/sessions/{session_id}/answer",
                      json={"answer": ans})
    elif t in ("completed", "aborted", "failed"):
        print("Terminal:", data)
        break
```

`curl` works too:

```bash
# Create + run
SID=$(curl -s -X POST localhost:8000/agent/sessions \
  -H 'Content-Type: application/json' \
  -d '{"goal":"open google.com and search for ai news"}' | jq -r .session_id)

curl -s -X POST localhost:8000/agent/sessions/$SID/run

# Stream events
curl -N localhost:8000/agent/sessions/$SID/events
```

**Key integration points:**

- **Events** — the SSE stream emits per-step events: `session_started`,
  `plan_created`, `step_screenshot` (base64 screenshot + URL/title/scroll),
  `thinking` (chain-of-thought), `action_executed`, `plan_step_completed`,
  `click_missed` / `action_repeat_warning` (action feedback),
  `waiting_for_user` (critical point), `safety_blocked`, `parse_retry` /
  `parse_error` / `execution_error`, and terminal `completed` / `aborted` /
  `failed`.
- **Critical points are mandatory** — the agent pauses on `ask_user_question`,
  `pause_and_memorize_fact`, and irreversible actions. Your app must handle
  `waiting_for_user` and call `/answer` (or `/resume`) or the session stays
  paused.
- **State polling** — `GET /agent/sessions/{id}` returns the full state
  (`state`, `goal`, `plan`, `plan_index`, `steps_completed`,
  `final_summary`, `trace`) for dashboards or when you can't hold an SSE
  connection.
- **Safety options** — pass `allowlist`/`blocklist` at session creation;
  navigation outside the allow-list raises `safety_blocked`.
- **Errors arrive as SSE data events** (`execution_error`, `parse_error`),
  not HTTP error codes — per SSE conventions the stream stays 200.
- **Model requirement** — the loop uses the active pool model; make sure a
  Fara-class vision model is loaded first (via `POST /models/select`).

## Safety (read this)

> **⚠️ This model drives a real web browser.** The Fara1.5-9B model card is explicit: if you integrate it directly, *you* are responsible for safety controls. Don't run this model with unrestricted browser access on a machine that has anything sensitive on it.

LlamaNet implements the model's **critical-point** design as a safety controller:

| Critical point | Behavior |
|---|---|
| **Missing user information** (personal info, payment details) | Agent pauses and asks — never fabricates |
| **Underspecified task** | Agent pauses and asks for clarification |
| **Irreversible action** (submit, purchase, send, delete) | Agent pauses and asks for confirmation before proceeding |

**Operating guidance:**

- Run on a dedicated machine, VM, or container when possible
- Prefer headless mode with a domain allow-list
- Use the watch-mode trace log (session events) to audit every action
- Use the **Stop** button (or `POST /agent/sessions/{id}/stop`) for an immediate halt
