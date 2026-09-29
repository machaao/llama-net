# API Reference

[← Back to README](../README.md)

LlamaNet exposes two API surfaces, both OpenAI-compatible:

- **Inference node** — runs on the machine with the GPU (`localhost:8000` or its tunnel URL). Used directly for local apps and by the [computer-use agent](agent.md).
- **Gateway** (`llamanet.app` or your self-hosted gateway) — routes API keys to the best node. Used by API consumers.

## Inference Node Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | List loaded models (pool-aware) |
| `POST` | `/v1/chat/completions` | Chat completion (streaming + reasoning supported) |
| `POST` | `/v1/completions` | Text completion |
| `GET` | `/health` | Health check (also used by the startup script) |
| `GET` | `/status` | Node metrics (load, TPS, TTFT, latency, tokens) |
| `GET` | `/info` | Node info (model, system, endpoints) |
| `GET` | `/events/network` | SSE stream of node/network events |
| `GET` | `/models/pool` | Multi-model pool state |
| `POST` | `/models/select` | Load/switch a model (instant if pooled) |
| `GET` | `/models/search` | Search Hugging Face for GGUF models |
| `POST` | `/models/download` | Start a model download (SSE progress at `/models/download/status`) |

### Computer-Use Agent (on the node)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/agent/sessions` | Create a session (goal + safety options) |
| `POST` | `/agent/sessions/{id}/run` | Start the loop (non-blocking) |
| `POST` | `/agent/sessions/{id}/pause` | Pause at the next step |
| `POST` | `/agent/sessions/{id}/resume` | Resume |
| `POST` | `/agent/sessions/{id}/answer` | Answer a critical-point question + resume |
| `POST` | `/agent/sessions/{id}/stop` | Abort + tear down the browser |
| `GET` | `/agent/sessions/{id}` | Session state + full trace log |
| `GET` | `/agent/sessions/{id}/events` | SSE stream of per-step events |

Full usage guide: [Computer-Use Agent](agent.md).

## Gateway Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/models` | List available models across the network (auth required) |
| `POST` | `/v1/chat/completions` | Chat completion — routed to the best node |
| `POST` | `/v1/completions` | Text completion — routed to the best node |
| `GET` | `/api/models` | Model catalog with node counts and metrics |
| `GET` | `/api/network/stats` | Network totals (nodes, models, tokens, TPS) |
| `GET` | `/events/network` | SSE stream (landing page live stats) |
| `GET` | `/auth/api-keys` / `POST` / `DELETE` | Manage API keys (dashboard) |
| `GET` | `/auth/token-usage` | Per-key token usage for the current period |

### Chat Completion Example

```bash
curl https://llamanet.app/v1/chat/completions \
  -H "Authorization: Bearer ln-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "Meta-Llama-3.1-8B-Instruct", "messages": [{"role": "user", "content": "Hello!"}]}'
```

Streaming (`"stream": true`) and reasoning (`"reasoning": true`) follow the OpenAI streaming format with an extra `reasoning_content` delta field.

## Environment Variables

### Inference Node

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_PATH` | — | Local GGUF path (alternative to `llamanet run <hf-url>`) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address |
| `NODE_ID` | auto | Stable node identifier |
| `BOOTSTRAP_PEERS` | `https://llamanet.app` | Gateway URL to join |
| `N_CTX` / `N_BATCH` / `N_UBATCH` / `N_PARALLEL` | `4096` / `256` / `512` / `1` | Performance tuning |
| `N_GPU_LAYERS` | `-1` | GPU layers (`0` = CPU-only) |
| `N_THREADS` / `N_THREADS_BATCH` | `0` (auto) | CPU threads for generation / prefill |
| `FLASH_ATTN` | `false` | FlashAttention |
| `CACHE_TYPE_K` / `CACHE_TYPE_V` | `f16` | KV cache quantization (`f16`, `q8_0`, `q4_0`) |
| `MAX_MODELS` / `MEMORY_BUDGET_GB` | auto | Multi-model pool sizing |
| `ENABLE_TUNNEL` / `LLAMANET_TUNNEL_URL` | `true` / — | Cloudflare tunnel control / fixed URL |
| `LOG_LEVEL` | `info` | Logging verbosity |

Command-line flags (`--ctx-size`, `--no-gpu`, …) override env vars. Full list: `llamanet --help`.

### Gateway

| Variable | Default | Purpose |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_SECRET_KEY` | — | Supabase connection (required) |
| `LLAMANET_APP_URL` | — | Public gateway URL (OAuth redirects) |
| `LLAMANET_DAILY_TOKEN_BUDGET` | `500000` | Tokens per key per day |
| `LLAMANET_MAX_KEY_CONCURRENT` | `3` | Concurrent requests per key |
| `LLAMANET_HOURLY_COMPUTE_BUDGET` | `10000` | Compute units per key per hour |
| `LLAMANET_REQUIRE_GPU` | `false` | Enable the node quality gate |
| `LLAMANET_MIN_TPS` / `LLAMANET_MAX_TTFT` / `LLAMANET_MAX_LATENCY` | `0.1` / `30` / `60` | Quality-gate thresholds |

### Docker / GPU Cloud

| Variable | Purpose |
|---|---|
| `MODEL_URL` | Hugging Face model to download and serve on start |
| `ENABLE_TUNNEL` | `true` (default) for a public URL |

See [Deployment](deployment.md) for provider-specific notes.

## Token Budgets & Rate Limits

Enforced at the gateway to keep the network fair:

| Control | Default | Behavior when exceeded |
|---|---|---|
| Daily token budget | 500,000 tokens / key / day | `429` — resets at midnight UTC |
| Key concurrency | 3 simultaneous requests / key | `429` with `Retry-After` |
| Hourly compute budget | 10,000 units / key / hour | `429` |
| Rate limiting | per-key and per-IP RPM | `429` with `Retry-After` |

Compute cost is estimated from requested `max_tokens` and model size (small models cost less). Budgets are configurable — see [Gateway & Enterprise Hosting](gateway.md) for team sizing.

## See Also

- [Models & Multi-Model Pool](models.md)
- [Computer-Use Agent](agent.md)
- [Gateway & Enterprise Hosting](gateway.md)
