# Frequently Asked Questions

[← Back to README](../README.md)

## What is LlamaNet?

LlamaNet is a decentralized AI inference network that lets anyone turn their Mac, GPU rig, or server into a free public AI API. It provides OpenAI-compatible endpoints, automatic Cloudflare tunnels for public URLs, and a built-in model manager to search and download models from Hugging Face. GPU owners share compute; API consumers use the network with standard OpenAI client libraries.

## How do I install LlamaNet?

**macOS / Linux:**

```bash
curl -sSL https://llamanet.app/install.sh | sh
```

**Windows (PowerShell):**

```powershell
irm https://llamanet.app/install.ps1 | iex
```

Then start a node with a model:

```bash
llamanet run hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M
```

The installer handles Python, dependencies, virtual environment, CLI shortcuts, and Desktop shortcuts automatically. See [Deployment](deployment.md) for per-platform details.

## What hardware do I need?

- **Apple Silicon (M1/M2/M3/M4):** 8GB+ RAM. Metal GPU acceleration is automatic — no drivers needed.
- **NVIDIA GPU:** 8GB+ VRAM (RTX 3070, 4060, 3090, 4090, etc.). Install CUDA drivers first.
- **CPU only:** Any modern CPU works with smaller models (Phi-4 Mini, Qwen 1.5B). Set `N_GPU_LAYERS=0`.

Larger RAM/VRAM lets you run larger, higher-quality models. See the tier table in [Models](models.md).

## Is LlamaNet free?

Yes. LlamaNet is free and open source under the Apache 2.0 license. GPU owners contribute compute from their own hardware. API consumers get free API keys at [llamanet.app](https://llamanet.app). There are no cloud GPU bills and no subscription fees — just rate limiting to prevent abuse.

## What models does LlamaNet support?

Any GGUF format model from Hugging Face: Llama, Qwen, Mistral, DeepSeek, Gemma, Phi, and hundreds more. The built-in Model Manager searches Hugging Face, downloads with real-time progress, and hot-swaps models without restarting your node. Multi-model pool mode keeps several models loaded simultaneously with LRU eviction. See [Models](models.md).

## How is LlamaNet different from Ollama or vLLM?

- **LlamaNet** is a distributed network. It connects independent nodes into one public API with automatic tunneling, gateway routing, and load balancing.
- **Ollama** runs models locally on your machine. Great for personal use, but others cannot access your models remotely.
- **vLLM** is a high-throughput serving engine designed for production deployments on dedicated GPU servers.

LlamaNet is for people who want to share their GPU with the world or consume a community-powered AI API — without managing infrastructure.

## How do networking and NAT traversal work?

Every LlamaNet node automatically creates a Cloudflare tunnel that provides a public HTTPS URL. No port forwarding, no Docker, no Kubernetes, no router configuration. The gateway tracks all nodes and routes API requests to the best available one based on load, latency, and model availability. Quick tunnels work without a Cloudflare account; named tunnels give you a persistent URL. See [Deployment](deployment.md#cloudflare-tunnels).

## Can I use LlamaNet commercially?

Yes. LlamaNet is licensed under Apache 2.0, which permits commercial use. You can run nodes, consume the API, and build applications on top of it. Individual model licenses are set by their creators on Hugging Face — check each model's license card before commercial use.

## Can my company run a private LlamaNet network?

Yes — the gateway is a small service you can host internally with your own keys, budgets, and node pool. See [Gateway & Enterprise Hosting](gateway.md#enterprise-internal-hosting-for-teams).

## How do I use the computer-use agent from my own app?

The agent (Fara1.5-9B) is a REST + SSE API on the inference node — create a session, run it, and consume per-step events. See [Computer-Use Agent](agent.md#using-the-agent-as-an-api-custom-apps).
