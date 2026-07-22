# LlamaNet: Decentralized Inference Swarm for llama.cpp

LlamaNet is a decentralized inference swarm for LLM models using llama.cpp. It provides an OpenAI-compatible API with real-time streaming, automatic node discovery via Kademlia DHT, and no single point of failure.

![LlamaNet](./static/images/screenshot-v2.png)

## Quick Start

### Installation

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt
```

> **Note:** `requirements-inference.txt` includes full inference support (`llama-cpp-python`, GPU detection, DHT, P2P). The base `requirements.txt` contains only lightweight gateway dependencies for the hosted landing page at [llamanet.app](https://llamanet.app).

### Launch LlamaNet

```bash
python -m inference_node.server
```

This starts LlamaNet in **no-model mode**. Open http://localhost:8000 and use the **Model Manager** to search, download, and select a model — all from the browser.

### Or Run a Model Directly

```bash
python -m inference_node.server run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
```

## URL Formats

```bash
hf.co/user/model                 # Latest
hf.co/user/model:Q4_K_M         # With quantization
user/model:Q4_K_M               # Short format
```

## Model Manager

Open the Web UI and click **Model Manager** to:
- Search Hugging Face for GGUF models
- View model details and available quantizations
- Download with real-time progress tracking
- Switch models without restarting (hot-reload)
- Manage and delete local models

## Access

- **Web UI**: http://localhost:8000
- **OpenAI API**: http://localhost:8000/v1/chat/completions

## API Example

```python
import openai

openai.api_base = "http://localhost:8000/v1"
openai.api_key = "dummy-key"

response = openai.ChatCompletion.create(
    model="llamanet",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.get("content"):
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Multi-Node Setup

```bash
# Node 1 (Bootstrap)
python -m inference_node.server --model-path ./models/model.gguf

# Node 2 (Join)
python -m inference_node.server \
  --model-path ./models/model.gguf \
  --port 8002 \
  --dht-port 8003 \
  --bootstrap-nodes localhost:8001
```

## GPU Provider: Run a Node & Join LlamaNet

If you have a GPU and want to contribute compute to the LlamaNet network, follow these steps.

### 1. Install Dependencies

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements-inference.txt
```

Verify your GPU is detected:

```bash
nvidia-smi
```

### 2. Run a Model

Pick any GGUF model from Hugging Face. LlamaNet handles downloading automatically:

```bash
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
```

Or with a specific quantization:

```bash
./start-app.sh run hf.co/TheBloke/Llama-2-7B-Chat-GGUF:Q4_K_M
```

This starts the node on `http://localhost:8000` with the Web UI and OpenAI-compatible API.

### 3. Connect to the LlamaNet Network

To make your node discoverable by others, connect to the public network using `--bootstrap-peers`:

```bash
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --tunnel \
  --bootstrap-peers https://llamanet.app
```

**What this does:**
- Downloads and runs the model on your GPU
- Starts a Cloudflare tunnel so your node is reachable from the internet
- Registers with the LlamaNet bootstrap peer at `llamanet.app`
- Other nodes discover you via DHT gossip

Your node will get a public URL like `https://abc-123.trycloudflare.com`.

### 4. Verify Your Node

Once running, check that everything is working:

```bash
# Local health check
curl http://localhost:8000/health

# Check your public tunnel URL
curl http://localhost:8000/tunnel/status

# View your node in the network
curl http://localhost:8000/nodes

# List peers that have discovered you
curl http://localhost:8000/peers
```

You can also open `http://localhost:8000` in your browser to use the Web UI, manage models, and chat.

### 5. GPU Configuration

LlamaNet auto-detects your GPU and uses all available layers by default. To tune:

| Env Variable | Default | Description |
|---|---|---|
| `N_GPU_LAYERS` | `-1` (all) | Layers to offload to GPU. Set lower if VRAM is limited. |
| `N_CTX` | `4096` | Context window in tokens. Increase for longer conversations. |
| `N_BATCH` | `4096` | Batch size. Higher values may improve throughput. |

Example with custom GPU settings:

```bash
N_GPU_LAYERS=32 N_CTX=8192 ./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --tunnel \
  --bootstrap-peers https://llamanet.app
```

### 6. Keep Your Node Running

For a persistent node, use `tmux` or `screen`:

```bash
tmux new -s llamanet
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --tunnel \
  --bootstrap-peers https://llamanet.app
# Detach: Ctrl+B, then D
# Reattach: tmux attach -t llamanet
```

### 7. Multiple Models (Multiple Nodes)

Run multiple models on different ports to contribute more capacity:

```bash
# Terminal 1: Llama 3.2 on port 8000
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --port 8000 --dht-port 8001 --tunnel --bootstrap-peers https://llamanet.app

# Terminal 2: Qwen on port 8002
./start-app.sh run hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M \
  --port 8002 --dht-port 8003 --tunnel --bootstrap-peers https://llamanet.app
```

Each node gets its own tunnel URL and is independently discoverable on the network.

### Troubleshooting GPU Nodes

```bash
# Verify GPU is accessible
nvidia-smi

# Check llama-cpp-python sees the GPU
python3 -c "from llama_cpp import Llama; print('OK')"

# Force CPU mode if GPU has issues
HARDWARE_MODE=cpu ./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M

# Check logs for GPU layer offloading
# Look for "Loading model with N_GPU_LAYERS" in output
```

## Cloudflare Tunnel (Internet-Accessible)

LlamaNet supports public URLs via Cloudflare tunnels — no port forwarding required.

### Quick Tunnel (No Account Needed)

```bash
./start-app.sh --tunnel
```

Generates a temporary URL (changes on restart). No Cloudflare account needed.

```
╔══════════════════════════════════════════════════════════════╗
║  🌍 Cloudflare Tunnel Active                                ║
║                                                              ║
║  Public URL: https://abc-123.trycloudflare.com              ║
║                                                              ║
║  Share this URL to access LlamaNet from anywhere.            ║
╚══════════════════════════════════════════════════════════════╝
```

### Named Tunnel (Persistent URL, Free Cloudflare Account)

A persistent URL that survives restarts. One-time setup via `cloudflared` CLI:

```bash
# 1. Login to Cloudflare (opens browser)
cloudflared tunnel login

# 2. Create a named tunnel
cloudflared tunnel create bootstrap

# 3. Route DNS — replace llamanet.app with your domain
cloudflared tunnel route dns bootstrap bootstrap.llamanet.app

# 4. Edit the generated config at ~/.cloudflared/config.yml:
```

```yaml
# ~/.cloudflared/config.yml
tunnel: <your-tunnel-uuid>
credentials-file: /Users/you/.cloudflared/<your-tunnel-uuid>.json

ingress:
  - hostname: bootstrap.llamanet.app
    service: http://localhost:8000
  - service: http_status:404
```

Then run LlamaNet with the tunnel:

```bash
./start-app.sh --tunnel
```

The script detects `~/.cloudflared/config.yml`, starts the named tunnel, and `https://bootstrap.llamanet.app` stays the same across restarts.

For additional nodes, repeat the process with different tunnel names:

```bash
cloudflared tunnel create gpu-m4
cloudflared tunnel route dns gpu-m4 gpu-m4.llamanet.app
# Update ~/.cloudflared/config.yml with new tunnel UUID and hostname
./start-app.sh --tunnel
```

### Connecting as an API Consumer

Point your OpenAI-compatible client at the tunnel URL:

```python
import openai

client = openai.OpenAI(
    base_url="https://bootstrap.llamanet.app/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="llamanet",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Connecting as a Peer Node

Use `--bootstrap-peers` to join the network via HTTP (works through tunnels):

```bash
./start-app.sh run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M \
  --tunnel \
  --bootstrap-peers https://bootstrap.llamanet.app
```

Your node registers with the bootstrap peer, discovers other peers via gossip, and becomes reachable at its own tunnel URL.

### How It Works

LlamaNet implements a **dual-transport DHT**:

- **UDP** — Used for LAN nodes (fast, zero-overhead)
- **HTTP** — Used for nodes behind Cloudflare tunnels, NAT, or firewalls

Both transports share the same Kademlia routing table. When a node registers via `--bootstrap-peers`, it joins the DHT over HTTP. Other nodes discover it through periodic gossip.

```
┌──────────┐  UDP  ┌──────────┐  HTTP  ┌──────────┐
│ Node A   │◄────►│ Node B   │◄─────►│ Node C   │
│ LAN      │      │ LAN +    │       │ Tunnel   │
│          │      │ bootstrap│       │          │
└──────────┘      └──────────┘       └──────────┘
```

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/peers` | List known peers |
| POST | `/peers/register` | Register as a peer |
| GET | `/tunnel/status` | Get tunnel status and public URL |
| POST | `/dht/rpc` | DHT protocol messages over HTTP |

## Docker Setup

LlamaNet includes Docker support with automatic GPU/CPU detection and no-model mode.

```bash
# Build and start a 3-node network
docker-compose -f docker/docker-compose.yml up -d

# Start a no-model router node (downloads model via Web UI)
docker run -d \
  --name llamanet-router \
  -p 8000:8000 \
  -p 8001:8001/udp \
  -v llamanet-models:/root/.llamanet/models \
  -e BOOTSTRAP_NODES="" \
  llamanet
```

See [docker/README.md](./docker/README.md) for full Docker documentation including GPU setup, environment variables, and scaling.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/models` | List models |
| POST | `/v1/chat/completions` | Chat completion |
| POST | `/v1/completions` | Text completion |
| GET | `/models/search?q=...` | Search HF models |
| POST | `/models/download` | Download model |
| GET | `/models/local` | List local models |
| POST | `/models/select` | Switch model |
| GET | `/health` | Health check |
| GET | `/status` | Node status |
| GET | `/events/network` | Network SSE |

## Features

- OpenAI-compatible API
- Model Manager with hot-reload
- No-model mode for first-time setup
- Chat format auto-detection
- Reasoning model support (DeepSeek-R1, etc.)
- Real-time streaming via SSE
- Kademlia DHT node discovery
- Hardware-based node IDs
- Load balancing (Round Robin, Load Balanced, Random)
- Request queuing
- System prompt customization

## Gated Models

```bash
huggingface-cli login
# Accept terms on Hugging Face website, then download via Model Manager or:
python -m inference_node.server run hf.co/LiquidAI/LFM2.5-1.2B-JP-202606-GGUF:Q4_K_M
```

## Troubleshooting

```bash
# Verify HF authentication
huggingface-cli whoami

# Manual download fallback
huggingface-cli download user/model file.gguf --local-dir ./models
python -m inference_node.server --model-path ./models/file.gguf
```

## Requirements

- Python 3.8+
- GGUF format models
- 4GB+ RAM (depends on model size)

### Dependency Files

| File | Purpose |
|------|---------|
| `requirements-inference.txt` | Full inference node — includes `llama-cpp-python`, GPU libs, DHT, P2P |
| `requirements.txt` | Lightweight gateway only — FastAPI, Supabase, no native compilation |

## Architecture

- HTTP API on port 8000 (OpenAI-compatible)
- DHT Network on port 8001 (peer discovery)
- Web UI at http://localhost:8000
- SSE for real-time updates (no polling)

## License

Apache License 2.0 - see [LICENSE](./LICENSE)

## Made with ❤️ using MACH-AI

Built with [MACH-AI](https://machai.live)
