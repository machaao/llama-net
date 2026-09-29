# Gateway & Enterprise Hosting

[← Back to README](../README.md)

The LlamaNet gateway is the coordination layer: node registry, request router, auth, API keys, budgets, and real-time events. The public network runs one at `llamanet.app` — but the gateway is a small FastAPI service you can run yourself, either for a private network or for **internal hosting inside a company/team**.

## How the Gateway Works

```
Nodes (GPU owners)                 Gateway                    API Consumers
──────────────────                 ───────                    ─────────────
llamanet run <model>  ──register──▶  node registry
                     ◀─heartbeat──   routing table
                     ──metrics────▶  budgets / rate limits   ──/v1/chat──▶ router ──▶ best node
                                     auth / API keys
                                     SSE event stream
```

- Nodes register with `--bootstrap-peers <gateway-url>`, send heartbeats and metrics, and get a per-node bearer token.
- API consumers call the gateway's OpenAI-compatible endpoints with an `ln-...` API key; the gateway routes to the best node for the requested model (load-balanced, round-robin, or random).
- Everything runs over HTTPS (Cloudflare tunnels by default) — no ports to open.

## Running Your Own Gateway

### 1. Set Up Supabase

The gateway uses Supabase for the node registry, users, API keys, and token accounting. Schema: [`sql/schema.sql`](../sql/schema.sql) — run it in the Supabase SQL editor.

### 2. Configure Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | yes | Supabase project URL |
| `SUPABASE_SECRET_KEY` | yes | Service-role key (server-side only — never ship to clients) |
| `SUPABASE_PUBLISHABLE_KEY` | no | Publishable key |
| `LLAMANET_APP_URL` | no | Public URL of the gateway (used for OAuth redirects, e.g. `https://llamanet.app`) |

### 3. Start the Gateway

```bash
pip install -e .
python -m gateway.server
```

Or via the startup script (gateway mode):

```bash
LLAMANET_MODE=landing sh start-app.sh
```

The gateway serves:
- `/` — landing page (public)
- `/dashboard` — API key management and node dashboard (Google sign-in)
- `/v1/models`, `/v1/chat/completions`, `/v1/completions` — OpenAI-compatible API
- `/health` — health check

### 4. Point Nodes at Your Gateway

```bash
llamanet run hf.co/user/Model:Q4_K_M --bootstrap-peers https://your-gateway.example.com
```

Installers default to the public network; pass `--bootstrap-peers` (or set `BOOTSTRAP_PEERS`) to join yours instead.

### Quality Gate

Set `LLAMANET_REQUIRE_GPU=true` to enforce two registration gates for nodes:

1. **Probe gate** — the node must complete a real inference within `LLAMANET_MIN_TPS` (default 0.1), with TTFT under `LLAMANET_MAX_TTFT` (default 30s) and end-to-end latency under `LLAMANET_MAX_LATENCY` (default 60s).
2. **Hardware gate** — NVIDIA GPUs (12 GB+ VRAM) or Apple Silicon (24 GB+ RAM) pass automatically; CPU-only nodes must report 32 GB+ RAM and 8+ CPU threads.

Enforced at registration; once admitted, nodes stay in. Keep it **off** for mixed hardware, **on** for a curated public pool.

## Enterprise: Internal Hosting for Teams

For companies, labs, and teams that want private inference — models and prompts stay on your network, with your own gateway, keys, and budgets. The whole setup takes one afternoon.

### What You Get

- **Data stays in** — inference runs on team machines; prompts never leave your network
- **One shared model catalog** — team members load models once, everyone uses them via the OpenAI API
- **Your own keys & budgets** — per-key daily token budgets, hourly compute budgets, concurrency limits
- **Curated node pool** — optional quality gate so only machines that meet spec join
- **Drop-in compatibility** — any OpenAI client pointed at your gateway works

### Reference Topology

```
Team laptops / GPU workstations                Internal network
───────────────────────────────                ─────────────────
Dev laptop:   llamanet run <model>  ──┐
GPU rig:      llamanet run <model>  ──┼──────▶ Gateway (1 VM/container)
Team server:  llamanet run <model>  ──┘        + Supabase (managed or self-hosted)
                                                │
CI / internal apps: openai.OpenAI(base_url=...)─┘
```

The gateway is lightweight (a small VM or container is plenty); nodes do the heavy lifting on team hardware.

### Option A — Managed Supabase + Docker Gateway (fastest)

1. **Create a Supabase project** and run [`sql/schema.sql`](../sql/schema.sql) in the SQL editor.
2. **Deploy the gateway** with the bundled compose file:

   ```bash
   cd deploy
   cp gateway.env.example .env    # fill in SUPABASE_URL, SUPABASE_SECRET_KEY, ...
   docker compose -f docker-compose.gateway.yml up -d
   ```

   See [`deploy/docker-compose.gateway.yml`](../deploy/docker-compose.gateway.yml).
3. **Verify**: `curl https://your-gateway/health` → `{"status": "ok", ...}`.
4. **Onboard a node** (any team machine):

   ```bash
   curl -sSL https://llamanet.app/install.sh | sh
   llamanet run hf.co/user/Model:Q4_K_M --bootstrap-peers https://your-gateway
   ```
5. **Issue API keys** — team members sign in at `https://your-gateway/dashboard` (Google OAuth) and create keys, or create them via `POST /auth/api-keys`.
6. **Call it**:

   ```python
   client = openai.OpenAI(
       base_url='https://your-gateway/v1',
       api_key='ln-...'
   )
   ```

### Option B — Fully Self-Hosted (no data leaves the premises)

Same as Option A, but run Supabase itself internally with the official
[self-hosted stack](https://supabase.com/docs/guides/self-hosting) (Docker). Point `SUPABASE_URL` at the internal instance — everything else is identical.

For fully private nodes (no Cloudflare tunnels), set each node's public URL manually:

```bash
LLAMANET_TUNNEL_URL=https://gpu-box.internal.corp:8000 \
llamanet run hf.co/user/Model:Q4_K_M --no-tunnel --bootstrap-peers https://your-gateway
```

The gateway only needs to reach the node URL for routing — an internal DNS name or VPN address works. With `--no-tunnel`, nothing is exposed outside your network.

### Team Rollout Cheat Sheet

| Who | What they run |
|---|---|
| **Platform owner** (1 person) | Supabase project + gateway (Option A or B), sets budgets |
| **GPU owners** | `llamanet run <model> --bootstrap-peers https://your-gateway` |
| **Developers / apps** | Any OpenAI client with `base_url=https://your-gateway/v1` and an `ln-...` key |

Sensible starting budgets for a 10–20 person team:

| Knob | Default | Suggested |
|---|---|---|
| `LLAMANET_DAILY_TOKEN_BUDGET` | 500,000 tokens/key/day | 1M–2M for heavy use |
| `LLAMANET_MAX_KEY_CONCURRENT` | 3 concurrent/key | 5 if nodes have headroom |
| `LLAMANET_HOURLY_COMPUTE_BUDGET` | 10,000 units/key/hour | keep default |
| `LLAMANET_REQUIRE_GPU` | off | `true` if all nodes are GPU-class |

### Operations

- **Health**: `GET /health` for uptime checks; stale nodes are cleaned automatically (heartbeat timeout).
- **Backups**: Supabase handles the managed path; for self-hosted, back up the Postgres volume.
- **Upgrades**: `git pull && pip install -e .` and restart — schema changes ship in `sql/schema.sql` and are additive.
- **Monitoring**: gateway logs go to stdout; `/events/network` SSE powers the dashboard's live view.

### Security Notes

- `SUPABASE_SECRET_KEY` lives only in the gateway environment — never in nodes or clients.
- Keep the gateway behind your TLS termination (internal CA, load balancer, or Cloudflare in front).
- Per-key token budgets and rate limits protect against runaway scripts (see [API Reference](api-reference.md#token-budgets--rate-limits)).
- For stricter control, front the gateway with your SSO/VPN and issue keys only to team members.

## See Also

- [API Reference](api-reference.md) — endpoints, budgets, rate limits
- [Deployment](deployment.md) — node setup on every platform
- [Models](models.md) — what the team can run
