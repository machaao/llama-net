# LlamaNet: Share Your GPU With The World

Run any open-source LLM on your hardware. Get a public API in 60 seconds.

![LlamaNet](./static/images/screenshot-v2.png)

## Quick Start

### Install (One Command)

```bash
# macOS / Linux
curl -sSL https://llamanet.app/install.sh | sh

# Windows (PowerShell)
irm https://llamanet.app/install.ps1 | iex
```

The installer:
- Detects or installs Python 3.9+
- Creates an isolated venv at `~/.llamanet/venv`
- Installs LlamaNet and llama-cpp-python
- Creates a `llamanet` CLI launcher and Desktop shortcut
- Auto-joins the public network at **llamanet.app**

Then run:

```bash
llamanet
```

1. Open **http://localhost:8000** — the Model Manager opens automatically
2. Search for a GGUF model (e.g. `qwen`, `llama`, `mistral`)
3. Click **Download** — progress streams in real time
4. Click **Use** — the model hot-loads and your node joins the network

You can also pre-load a model at startup:

```bash
llamanet run hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M
```

Or install via PyPI:

```bash
pip install llamanet
llamanet run hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M
```

Models can be switched at any time via the Web UI without restarting the node.

### Uninstall

```bash
# macOS / Linux
curl -sSL https://llamanet.app/uninstall.sh | sh

# Windows (PowerShell)
irm https://llamanet.app/uninstall.ps1 | iex
```

### API Consumer — Use the Network

```python
import openai

client = openai.OpenAI(
    base_url="https://llamanet.app/v1",
    api_key="your-api-key"
)

response = client.chat.completions.create(
    model="Ministral-3-8B-Instruct",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)
```

Get a free API key at [llamanet.app](https://llamanet.app).

## How It Works

```
GPU Owner                          API Consumer
─────────                          ────────────
curl -sSL llamanet.app/ \           client = openai.OpenAI(
  install.sh | sh                    base_url="https://llamanet.app/v1",
llamanet run \                       api_key="ln-xxx"
  hf.co/user/Model:Q4_K_M         )
       │                                │
       ▼                                ▼
┌─────────────────────────────────────────────┐
│           llamanet.app (Gateway)             │
│                                              │
│  Node Registry · Request Router · Auth       │
│  Real-time SSE · Model Discovery             │
└─────────────────────────────────────────────┘
```

1. **GPU owner** runs the one-liner installer → `llamanet` → node auto-joins `llamanet.app`
2. Downloads a model via the Web UI Model Manager
3. Node gets a Cloudflare tunnel URL (public, HTTPS) — **required for network participation**
4. **API consumer** calls `llamanet.app/v1/chat/completions`
5. Gateway routes to the best available node via tunnel URL

No Docker. No Kubernetes. No port forwarding. Just a tunnel and a registry.

## Features

- **No-Model Mode** — Start your node instantly, download models later via the Web UI
- **Hot Reload** — Switch models without restarting your node — download, select, done
- **OpenAI-Compatible API** — Drop-in replacement for any OpenAI client
- **Free Public URLs** — Automatic Cloudflare tunnels, zero config
- **Web UI** — Built-in model manager, chat interface, and network dashboard at `localhost:8000`
- **Model Manager** — Search Hugging Face, download GGUF models with real-time progress, switch models without restarting
- **Model Discovery** — Search and connect to models across the network
- **Reasoning Support** — DeepSeek-R1, Qwen reasoning models with streaming reasoning content
- **Gateway Routing** — Central gateway with automatic node discovery and load balancing
- **Real-time Network** — SSE-powered live updates for node status, model availability, and metrics
- **GPU Auto-detect** — NVIDIA, Apple Silicon, CPU fallback

## Multi-Model Pool

LlamaNet supports loading multiple models simultaneously in a pool with automatic LRU (Least Recently Used) eviction. This lets you:

- **Instant model switching** — switch between loaded models without reload time
- **Memory-aware capacity** — auto-detects how many models fit in your RAM/VRAM
- **LRU eviction** — when pool is full, the least recently used model is evicted to make room

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MODELS` | `0` (auto) | Max models in memory (0 = auto-detect from RAM) |
| `MEMORY_BUDGET_GB` | `0` (auto) | Max RAM for models (0 = auto-detect) |

### How It Works

1. **First model** loads normally on startup (or via Web UI)
2. **Loading a second model** adds it to the pool (if capacity allows)
3. **When pool is full**, the least recently used model is evicted
4. **Switching between pool models** is instant — no reload overlay
5. **Pool state persists** across restarts via `~/.llamanet/pool_history.json`

## Recommended Models by Tier

| Tier | Model | Size (Q4_K_M) | Min RAM | Best For |
|------|-------|---------------|---------|----------|
| **Tiny** | Phi-4 Mini | ~1.5 GB | 8 GB | Quick tasks, edge devices, fast iteration |
| **Fast** | Qwen 4B | ~2.5 GB | 8 GB | Daily driver — fast responses, solid quality |
| **Standard** | Ministral 3 8B | ~4.5 GB | 16 GB | Best all-round balance of speed and quality |
| **Coding** | Qwen 14B | ~8 GB | 32 GB | Code generation, technical tasks |
| **Premium** | GPT-OSS 20B | ~12 GB | 32 GB | High-quality general-purpose inference |
| **High Quality** | Qwen 35B-A3B | ~20 GB | 48 GB | Expert-level reasoning, long-form generation |
| **Reasoning** | DeepSeek-R1 Distill 32B | ~18 GB | 48 GB | Chain-of-thought reasoning, math, analysis |

### Which Tier Fits Your Hardware?

| Your Hardware | Recommended Tiers | Notes |
|---------------|-------------------|-------|
| Mac M1/M2 8GB | Tiny, Fast | Use Q4_K_M quantization |
| Mac M1/M2 Pro 16GB | Tiny → Standard | Standard tier runs great on Pro chips |
| Mac M3/M4 Pro 18GB+ | Tiny → Coding | 18GB unified memory handles up to 14B models |
| Mac M2/M3 Max 32GB+ | All tiers | Max chips handle even the largest models |
| Mac M2 Ultra 64GB+ | All tiers | No compromises — run anything |
| NVIDIA 8GB VRAM | Tiny, Fast | RTX 3070, 4060, etc. |
| NVIDIA 12GB VRAM | Tiny → Standard | RTX 3060 12GB, 4070 |
| NVIDIA 24GB VRAM | Tiny → Premium | RTX 3090, 4090 |
| NVIDIA 48GB+ VRAM | All tiers | A6000, A100, multi-GPU setups |

## Architecture

LlamaNet uses a **gateway-centric, tunnel-only** architecture:

- **Gateway** (`llamanet.app`) — Central registry, authentication, request routing
- **Inference Nodes** — GPU owners running models behind Cloudflare tunnels
- **Tunnel URLs** — Every node has a public HTTPS URL. No IP:port addressing.

All peer discovery and communication goes through the gateway. There is no peer-to-peer networking, DHT, or distributed hash table. This keeps the system simple, reliable, and NAT-friendly.

## Web UI

Every inference node serves a built-in web UI at `http://localhost:8000`:

- **Model Manager** — Search Hugging Face for GGUF models, download with real-time progress, switch models without restarting. Opens automatically when no model is loaded.
- **Chat Interface** — Talk to your model with streaming responses and markdown rendering
- **Network Dashboard** — See all connected nodes, models, and real-time metrics via SSE
- **System Prompt** — Configure custom system prompts with presets

The typical workflow is: start your node → open the Web UI → download a model → chat. Switching models is instant — no restart required.

**No-Model Mode:** When started without a model, the node launches in router-only mode. It can still forward requests to other nodes on the network. Use the Model Manager to download and hot-load a model when ready.

## URL Formats

These formats work with `./start-app.sh run` and the Web UI Model Manager:

```bash
hf.co/user/model                 # Latest
hf.co/user/model:Q4_K_M         # With quantization
user/model:Q4_K_M               # Short format
```

The Model Manager also accepts plain search queries — just type a model name and browse results.

## Cloudflare Tunnels

A tunnel URL is **required** to join the LlamaNet network. The `--tunnel` flag handles everything automatically.

### Quick Tunnel (No Account Needed)

```bash
sh start-app.sh run hf.co/user/Model:Q4_K_M --tunnel
```

Generates a temporary URL that changes on restart. No Cloudflare account needed.

### Named Tunnel (Persistent URL, Free Account)

```bash
cloudflared tunnel login
cloudflared tunnel create bootstrap
cloudflared tunnel route dns bootstrap bootstrap.llamanet.app
sh start-app.sh run hf.co/user/Model:Q4_K_M --tunnel
```

The URL persists across restarts.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PATH` | — | Path to GGUF model file |
| `PORT` | `8000` | HTTP API port |
| `HOST` | `0.0.0.0` | Bind address |
| `N_GPU_LAYERS` | `-1` | GPU layers (-1 = all) |
| `N_CTX` | `4096` | Context window in tokens |
| `N_BATCH` | `4096` | Batch size |
| `BOOTSTRAP_PEERS` | `https://llamanet.app` | Gateway URL (auto-set by installer) |
| `PUBLIC_IP` | — | Override public IP detection |
| `LLAMANET_TUNNEL_URL` | — | Override tunnel URL |

## API Reference

### Inference Node (local)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | Chat completion (streaming supported) |
| `/v1/completions` | POST | Text completion (streaming supported) |
| `/v1/models` | GET | List local model |
| `/v1/models/network` | GET | List all models across the network |
| `/models/search` | GET | Search Hugging Face for GGUF models |
| `/models/download` | POST | Start downloading a model |
| `/models/select` | POST | Switch to a different model |
| `/events/network` | GET | SSE stream for real-time network events |
| `/health` | GET | Health check |
| `/tunnel/status` | GET | Tunnel status and URL |

### Gateway (`llamanet.app`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | Route chat completion to best node |
| `/v1/completions` | POST | Route completion to best node |
| `/v1/models` | GET | List all available models |
| `/api/models` | GET | Public model listing |
| `/api/network/stats` | GET | Network statistics |
| `/events/network` | GET | SSE stream for network events |
| `/auth/google` | GET | Google OAuth login |

## Setup

### macOS

**Apple Silicon (M1/M2/M3/M4)** — full GPU acceleration via Metal, no extra steps:

```bash
# One-liner install (recommended)
curl -sSL https://llamanet.app/install.sh | sh
llamanet
```

Or install from source:

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt
sh start-app.sh --tunnel --bootstrap-peers https://llamanet.app
```

**Intel Macs** — Metal shaders in `llama-cpp-python` 0.3.x are incompatible with Intel GPUs.
LlamaNet auto-detects Intel Macs and disables Metal at startup (CPU-only mode). No manual configuration needed.

If you need to force it manually:

```bash
export LLAMA_NO_METAL=1
llamanet
```

You can also pass `--no-gpu` to the inference node directly:

```bash
python -m inference_node.server --no-gpu --tunnel --bootstrap-peers https://llamanet.app
```

### Linux

Works on any Linux distro with Python 3.9+. For NVIDIA GPUs, ensure drivers and CUDA are installed:

```bash
# One-liner install (recommended)
curl -sSL https://llamanet.app/install.sh | sh
llamanet
```

Or install from source:

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt
sh start-app.sh --tunnel --bootstrap-peers https://llamanet.app
```

For CPU-only or non-NVIDIA setups:

```bash
N_GPU_LAYERS=0 llamanet
```

### Windows

Works on Windows 10/11 with Python 3.9+. For NVIDIA GPUs, ensure drivers and CUDA are installed:

```powershell
# One-liner install (recommended)
irm https://llamanet.app/install.ps1 | iex
llamanet
```

Or install from source:

```cmd
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt
start-app.bat --tunnel --bootstrap-peers https://llamanet.app
```

For CPU-only mode:

```cmd
set N_GPU_LAYERS=0
start-app.bat --tunnel --bootstrap-peers https://llamanet.app
```

**Notes:**
- Use `start-app.bat` instead of `sh start-app.sh` on Windows
- Python must be in your PATH (install from python.org with "Add to PATH" checked)
- `cloudflared` for tunnels can be downloaded from https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/

### Cloud / Remote Operators

Run a node on any VPS or cloud instance (AWS, GCP, Azure, Hetzner, etc.):

```bash
curl -sSL https://llamanet.app/install.sh | sh
llamanet run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
```

**GPU instances:** Install NVIDIA drivers + CUDA toolkit before running. The node auto-detects GPU availability.

**CPU instances:** Set `N_GPU_LAYERS=0` or use `--no-gpu`. Smaller models (≤3B parameters) work well on CPU.

**Persistent tunnel:** For a stable URL across restarts, set up a named Cloudflare tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create my-node
cloudflared tunnel route dns my-node node.mydomain.com
llamanet run hf.co/user/Model:Q4_K_M --tunnel
```

### Operating Your Own Rig

For dedicated GPU machines (desktop, workstation, or server):

1. **Install with one command:**
   ```bash
   curl -sSL https://llamanet.app/install.sh | sh
   ```

2. **Start with a model:**
   ```bash
   llamanet run hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M
   ```

3. **Or start empty and download via Web UI:**
   ```bash
   llamanet
   ```
   Open `http://localhost:8000` → Model Manager → search → download → chat.

4. **Switch models at any time** via the Web UI without restarting.

**Tips:**
- Use `--ctx-size` to control context window (default: 4096)
- Use `--gpu-layers -1` to offload all layers to GPU (default)
- Use `--gpu-layers N` to split between GPU and CPU for large models
- The node auto-generates a persistent ID stored in `~/.llamanet_node_id`
- The node auto-joins the public network at `llamanet.app` (override with `--bootstrap-peers`)

## Requirements

- Python 3.9+
- GGUF format models
- 4GB+ RAM (depends on model size)
- `cloudflared` (auto-installed by the installer and `start-app.sh` on macOS/Linux)
