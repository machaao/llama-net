# Models & Multi-Model Pool

[← Back to README](../README.md)

LlamaNet runs any GGUF model hosted on Hugging Face. This guide covers model selection, hardware tiers, supported URL formats, and the multi-model pool.

## Supported Models

Any **GGUF** model works: Llama (3.x, 3.2 Vision), Qwen (2.5, 3, Qwen-VL), Mistral / Mixtral, Phi (3, 4), Gemma (2, 3), DeepSeek-R1 (and distills), Command-R, Yi, LLaVA, InternLM, SmolLM, OLMo, Fara (computer-use), and hundreds more.

To add a model:

1. Open the Web UI at `http://localhost:8000`
2. Click **Model Manager** → **Search**
3. Click **Download** on any GGUF model

The Model Manager shows real-time download progress (speed, ETA, MB downloaded) and automatically fits recommendations to your RAM/VRAM.

## Recommended Models by Tier

| Tier | Model | Size (Q4) | Min RAM | Best For |
|---|---|---|---|---|
| Tiny | **Phi-4 Mini** | ~1.5 GB | 8 GB | Quick tasks, edge devices, fast iteration |
| Fast | **Qwen 4B** | ~2.5 GB | 8 GB | Daily driver — fast responses, solid quality |
| Standard | **Ministral 3 8B** | ~4.5 GB | 16 GB | Best all-round balance of speed and quality |
| Versatile | **Gemma 3 12B** | ~7 GB | 24 GB | General-purpose, multilingual, strong reasoning |
| Coding | **Qwen 14B** | ~8 GB | 32 GB | Code generation, technical tasks |
| Premium | **GPT-OSS 20B** | ~12 GB | 32 GB | High-quality general-purpose inference |
| High Quality | **Qwen 35B-A3B** | ~20 GB | 48 GB | Expert-level reasoning, long-form generation |
| Reasoning | **DeepSeek-R1 Distill 32B** | ~18 GB | 48 GB | Chain-of-thought reasoning, math, analysis |

Sizes are approximate for Q4_K_M quantization. Apple Silicon uses unified memory.

### Which Tier Fits Your Hardware

| Hardware | What You Can Run |
|---|---|
| **Apple Silicon M1** (8 GB) | Tiny / Fast |
| **Apple Silicon M2** (8/16 GB) | Tiny / Fast / Standard |
| **Apple Silicon M3** (8/16/24 GB) | Tiny / Fast / Standard / Versatile |
| **Apple Silicon M4 Pro** (24/48 GB) | Up to Premium |
| **Apple Silicon M4 Max** (36/48/64/128 GB) | All tiers |
| **Apple Silicon M3/M4 Ultra** (96/192/512 GB) | All tiers + 70B models |
| **NVIDIA RTX 4060** (8 GB VRAM) | Tiny / Fast |
| **NVIDIA RTX 4090** (24 GB VRAM) | Up to Premium |
| **NVIDIA A100** (80 GB VRAM) | All tiers + 70B models |
| **NVIDIA H100** (80 GB VRAM) | All tiers + 70B+ models |
| **CPU (8+ cores, 16 GB+ RAM)** | Tiny / Fast |
| **CPU (16+ cores, 32 GB+ RAM)** | Tiny / Fast / Standard |

## URL Formats

Models can be specified in several formats (used with `llamanet run <url>`):

```
hf.co/user/model                 # Latest version
hf.co/user/model:Q4_K_M         # Specific quantization
hf.co/user/model@branch          # Specific branch
user/model                       # Short format
user/model:Q4_K_M                # Short format with quantization
https://huggingface.co/user/model:Q4_K_M   # Full URL
```

**Quantizations:** Q2_K (smallest), Q3_K_S/M/L, Q4_0, Q4_K_S, **Q4_K_M (recommended)**, Q5_0, Q5_K_S, Q5_K_M, Q6_K, Q8_0 (highest quality)

## Multi-Model Pool

Keep multiple models loaded simultaneously with automatic LRU eviction. Switch between them instantly without downloading or reloading.

### How It Works

- **Capacity auto-detected** from your available RAM (or set `--max-models N`)
- **LRU eviction** — loading a new model when the pool is full evicts the least recently used one
- **Instant switch** — models stay in memory; switching is instant with no reload
- **Pool management** — view all loaded models in the Model Manager's **Pool** tab; unload any model manually

### Configuration

| Flag / Env | Default | Description |
|---|---|---|
| `--max-models N` / `MAX_MODELS` | auto-detect | Max models to keep loaded (0 = auto-detect from RAM) |
| `--memory-budget-gb N` / `MEMORY_BUDGET_GB` | auto-detect | Max RAM for models in GB (0 = auto-detect) |

Examples:

```bash
# Keep up to 3 models loaded
llamanet run hf.co/user/Model:Q4_K_M --max-models 3

# Cap model memory at 24 GB
llamanet run hf.co/user/Model:Q4_K_M --memory-budget-gb 24
```

Switch models at any time from the Web UI (**Model Manager → Pool**) without restarting the node.

## See Also

- [API Reference](api-reference.md) — model endpoints (`/v1/models`, `/models/pool`)
- [Deployment](deployment.md) — hardware-specific setup
