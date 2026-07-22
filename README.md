# LlamaNet: Share Your GPU With The World

Run any open-source LLM on your hardware. Get a public API in 60 seconds.

![LlamaNet](./static/images/screenshot-v2.png)

## Quick Start

### GPU Owner — Run a Model

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt

# Run a model and join the network
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --tunnel \
  --bootstrap-peers https://llamanet.app
```

Your node is now live with a public URL and discoverable by anyone.

### API Consumer — Use the Network

```python
import openai

client = openai.OpenAI(
    base_url="https://llamanet.app/v1",
    api_key="your-api-key"
)

response = client.chat.completions.create(
    model="Llama-3.2-3B-Instruct",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)
```

Get a free API key at [llamanet.app](https://llamanet.app).

## How It Works

```
GPU Owner                          API Consumer
─────────                          ────────────
./start-app.sh run \               client = openai.OpenAI(
  hf.co/user/Model:Q4_K_M \         base_url="https://llamanet.app/v1",
  --tunnel \                         api_key="ln-xxx"
  --bootstrap-peers \              )
  https://llamanet.app             
       │                                │
       ▼                                ▼
┌─────────────────────────────────────────────┐
│           llamanet.app (Gateway)             │
│                                              │
│  Node Registry · Request Router · Auth       │
│  Real-time SSE · Model Discovery             │
└─────────────────────────────────────────────┘
```

1. **GPU owner** runs a model with `--tunnel --bootstrap-peers`
2. Gets a Cloudflare tunnel URL (public, HTTPS)
3. Registers with the gateway at `llamanet.app`
4. **API consumer** calls `llamanet.app/v1/chat/completions`
5. Gateway routes to the best available node

No Docker. No Kubernetes. No port forwarding. Just a tunnel and a registry.

## Features

- **OpenAI-Compatible API** — Drop-in replacement for any OpenAI client
- **Free Public URLs** — Automatic Cloudflare tunnels, zero config
- **Model Discovery** — Search and connect to models across the network
- **Hot Reload** — Switch models without restarting your node
- **Reasoning Support** — DeepSeek-R1, Qwen reasoning models
- **Load Balancing** — Auto-route to the best available node
- **GPU Auto-detect** — NVIDIA, Apple Silicon, CPU fallback

## URL Formats

```bash
hf.co/user/model                 # Latest
hf.co/user/model:Q4_K_M         # With quantization
user/model:Q4_K_M               # Short format
```

## Model Manager

Open the Web UI at `http://localhost:8000` and click **Model Manager** to:
- Search Hugging Face for GGUF models
- Download with real-time progress tracking
- Switch models without restarting

## Cloudflare Tunnels

### Quick Tunnel (No Account Needed)

```bash
./start-app.sh --tunnel
```

Generates a temporary URL that changes on restart. No Cloudflare account needed.

### Named Tunnel (Persistent URL, Free Account)

```bash
cloudflared tunnel login
cloudflared tunnel create bootstrap
cloudflared tunnel route dns bootstrap bootstrap.llamanet.app
./start-app.sh --tunnel
```

The URL persists across restarts.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PATH` | — | Path to GGUF model file |
| `PORT` | `8000` | HTTP API port |
| `N_GPU_LAYERS` | `-1` | GPU layers (-1 = all) |
| `N_CTX` | `4096` | Context window in tokens |
| `N_BATCH` | `4096` | Batch size |

## Requirements

- Python 3.8+
- GGUF format models
- 4GB+ RAM (depends on model size)

## License

Apache License 2.0 — see [LICENSE](./LICENSE)

## Made with ❤️ using MACH-AI

Built with [MACH-AI](https://machai.live)
