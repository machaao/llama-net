# LlamaNet: Decentralized Inference Swarm for llama.cpp

LlamaNet is a decentralized inference swarm for LLM models using llama.cpp. It provides an OpenAI-compatible API with real-time streaming, automatic node discovery via Kademlia DHT, and no single point of failure.

![LlamaNet](./static/images/screenshot-v2.png)

## Quick Start

### Installation

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements.txt
```

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

## Architecture

- HTTP API on port 8000 (OpenAI-compatible)
- DHT Network on port 8001 (peer discovery)
- Web UI at http://localhost:8000
- SSE for real-time updates (no polling)

## License

Apache License 2.0 - see [LICENSE](./LICENSE)

## Made with ❤️ using MACH-AI

Built with [MACH-AI](https://machai.live)
