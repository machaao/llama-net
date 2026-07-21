# LlamaNet: Decentralized Inference Swarm for llama.cpp

LlamaNet is a decentralized inference swarm for LLM models using llama.cpp. It provides an OpenAI-compatible API with real-time streaming, automatic node discovery via Kademlia DHT, and no single point of failure.

![LlamaNet](./static/images/screenshot.png)

## Quick Start

### 1. Installation

```bash
git clone https://github.com/machaao/llama-net.git
cd llama-net
pip install -r requirements.txt
```

### 2. Download and Run a Model

```bash
# Using Python module (recommended)
python -m inference_node.server run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M

# Or using start-app.sh
./start-app.sh run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M
```

### 3. Start Without a Model (No-Model Mode)

```bash
# Start the node without a model - the Web UI will open to download one
python -m inference_node.server
```

The Web UI will display a welcome banner prompting you to use the **Model Manager** to download and select a model.

### 4. Access the API

- **Web UI**: http://localhost:8000
- **OpenAI API**: http://localhost:8000/v1/chat/completions
- **Models List**: http://localhost:8000/v1/models

## Supported URL Formats

```bash
hf.co/user/model                 # Latest version
hf.co/user/model:Q4_K_M         # With quantization
user/model:Q4_K_M               # Short format
https://huggingface.co/user/model:Q4_K_M  # Full URL
```

## Model Manager

LlamaNet includes a built-in **Model Manager** accessible from the Web UI. It allows you to search, download, and manage GGUF models directly from the browser without restarting the server.

### Features

- **Search Hugging Face** - Search for GGUF models by name, with quick preset buttons (Llama, Mistral, Phi, Qwen)
- **Model Details** - View model information, GGUF files, downloads, and likes before downloading
- **Download with Progress** - Real-time SSE-based download progress with speed, bytes downloaded, and percentage
- **Cancel Downloads** - Cancel active downloads at any time
- **Local Model Management** - List all cached models with disk usage, select models for use, or delete from disk
- **Hot-Reload** - Switch between models without restarting the server. The LLM is unloaded and reloaded in-place
- **No-Model Mode** - Start the server without a model and use the Model Manager to download one

### Accessing the Model Manager

1. Click the **Model Manager** button in the navbar, or
2. If no model is loaded, the welcome banner will prompt you to open it automatically

## Working with Gated Models

Some models require authentication:

```bash
# 1. Get token from https://huggingface.co/settings/tokens
huggingface-cli login

# 2. Accept model terms on Hugging Face website
# Visit: https://huggingface.co/LiquidAI/LFM2.5-1.2B-JP-202606-GGUF
# Click "Access repository" and accept terms

# 3. Download and run
python -m inference_node.server run hf.co/LiquidAI/LFM2.5-1.2B-JP-202606-GGUF:Q4_K_M
```

## Running Multiple Nodes

```bash
# Node 1 (Bootstrap)
python -m inference_node.server --model-path ./models/model.gguf

# Node 2 (Join network)
python -m inference_node.server \
  --model-path ./models/model.gguf \
  --port 8002 \
  --dht-port 8003 \
  --bootstrap-nodes localhost:8001
```

## OpenAI-Compatible API

```python
import openai

openai.api_base = "http://localhost:8000/v1"
openai.api_key = "dummy-key"

# Chat completion
response = openai.ChatCompletion.create(
    model="llamanet",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.get("content"):
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Reasoning Model Support

LlamaNet automatically detects reasoning models (e.g., DeepSeek-R1) and separates reasoning content from the final response. Reasoning tokens are streamed as `reasoning_content` alongside regular `content` in streaming responses.

```python
# Streaming with reasoning
response = openai.ChatCompletion.create(
    model="deepseek-r1",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
    stream=True
)

for chunk in response:
    delta = chunk.choices[0].delta
    if delta.get("reasoning_content"):
        # Process reasoning tokens
        print(f"[Reasoning] {delta.reasoning_content}", end="")
    if delta.get("content"):
        # Process final response tokens
        print(delta.content, end="")
```

## Chat Format Auto-Detection

LlamaNet automatically detects the appropriate chat format based on the model name. Supported formats include:

| Format | Typical Models |
|--------|---------------|
| `llama-3` | Llama 3.1, Llama 3.2 |
| `llama-2` | Llama 2 Chat |
| `chatml` | GPT, Yi, Qwen2 (default fallback) |
| `mistral-instruct` | Mistral, Mixtral |
| `gemma` | Google Gemma |
| `qwen` | Alibaba Qwen |
| `vicuna` | Vicuna |
| `alpaca` | Alpaca, WizardLM |
| `zephyr` | HuggingFace Zephyr |

## Recommended Models

### Getting Started (Small & Fast)
```bash
python -m inference_node.server run hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
```

### Production Use (Balanced)
```bash
python -m inference_node.server run hf.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M
```

### High Performance (Larger)
```bash
python -m inference_node.server run hf.co/unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M
```

## API Endpoints

### OpenAI-Compatible
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/models` | List available models with chat format info |
| GET | `/v1/models/network` | List all models across the network |
| POST | `/v1/completions` | Text completion |
| POST | `/v1/chat/completions` | Chat completion (streaming supported) |

### Model Manager
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/models/search?q=...` | Search Hugging Face for GGUF models |
| GET | `/models/details/{repo_id}` | Get model details and GGUF files |
| POST | `/models/download` | Start a model download |
| GET | `/models/download/status?download_id=...` | SSE stream for download progress |
| DELETE | `/models/download/{download_id}` | Cancel an active download |
| GET | `/models/local` | List all locally cached models |
| DELETE | `/models/local/{model_id}` | Delete a local model |
| POST | `/models/select` | Hot-reload to a different model |
| GET | `/models/statistics` | Get network model statistics |

### System
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/status` | Node status and metrics |
| GET | `/info` | Static node information |
| GET | `/events/network` | SSE stream for real-time network events |
| GET | `/queue/status` | Request queue status |

## Web UI Features

- **Chat Interface** - Interactive chat with streaming responses and markdown rendering
- **System Prompt** - Customizable system prompt with presets (Helpful, Creative, Technical, Teacher, Analyst)
- **Model Manager** - Download and manage models from the browser
- **Network Status** - Real-time network topology via SSE (no polling)
- **Node Discovery** - View all nodes, models, and availability across the network
- **Load Balancing** - Configurable strategies: Round Robin, Load Balanced, Random
- **No-Model Mode** - Graceful startup without a model, with guided download flow

## Troubleshooting

### 401 Unauthorized Error

```bash
# Authenticate with Hugging Face
huggingface-cli login

# Verify authentication
huggingface-cli whoami

# Accept model terms on Hugging Face website
```

### Model Not Found

```bash
# Verify model exists
# Visit: https://huggingface.co/search/models?q=your-model-name

# Use correct format: user/model-name
python -m inference_node.server run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M
```

### Manual Download Fallback

```bash
# Download using huggingface-cli
huggingface-cli download LiquidAI/LFM2.5-1.2B-JP-202606-GGUF \
  LFM2.5-1.2B-JP-202606-Q4_K_M.gguf \
  --local-dir ./models \
  --local-dir-use-symlinks False

# Run with local path
python -m inference_node.server --model-path ./models/LFM2.5-1.2B-JP-202606-Q4_K_M.gguf
```

## Common Commands

```bash
# Download and run a model
python -m inference_node.server run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M

# Start without a model (use Web UI to download one)
python -m inference_node.server

# Run with local model
python -m inference_node.server --model-path ./models/model.gguf

# Run with custom port
python -m inference_node.server run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M --port 8080

# Authenticate with Hugging Face
huggingface-cli login

# Check authentication status
huggingface-cli whoami
```

## Features

- **OpenAI-Compatible API** - Drop-in replacement for OpenAI endpoints
- **Model Manager** - Search, download, and manage models from the Web UI
- **No-Model Mode** - Start without a model, download one via the browser
- **Hot-Reload** - Switch models without restarting the server
- **Reasoning Model Support** - Automatic detection and separation of reasoning content
- **Chat Format Auto-Detection** - Detects the correct chat template from model name
- **Real-time Streaming** - Server-Sent Events for live responses
- **Decentralized Discovery** - Kademlia DHT for node discovery
- **Hardware-Based Node IDs** - Consistent identity across restarts
- **Multi-Node Support** - Automatic load balancing with configurable strategies
- **Request Queuing** - Sequential LLM processing with queue management
- **Web UI** - Interactive chat interface with markdown rendering and code highlighting
- **System Prompts** - Customizable system prompts with presets
- **No Single Point of Failure** - Fully distributed architecture

## Requirements

- Python 3.8+
- GGUF format models (compatible with llama.cpp)
- 4GB+ RAM (depends on model size)

## Architecture

- **HTTP API**: OpenAI-compatible endpoints on port 8000
- **DHT Network**: Peer discovery on port 8001
- **Web UI**: Interactive interface at http://localhost:8000
- **Model Manager**: Browser-based model download and management
- **Request Queue**: Sequential LLM processing with graceful reload support
- **SSE**: Real-time network updates (no polling)
- **Streaming**: Real-time responses via Server-Sent Events

## License

Apache License 2.0 - see [LICENSE](./LICENSE) file for details.

## Made with ❤️ using MACH-AI

This project was built with [MACH-AI](https://machai.live)
