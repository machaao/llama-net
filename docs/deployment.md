# Deployment & Platform Setup

[← Back to README](../README.md)

LlamaNet runs everywhere Python runs: Mac, Linux, Windows, cloud VPS, and GPU cloud providers (RunPod, vast.ai). This guide covers platform-specific setup and cloud deployment.

## macOS (Apple Silicon)

The one-line installer handles everything:

```bash
curl -sSL https://llamanet.app/install.sh | sh
llamanet run hf.co/user/Model:Q4_K_M
```

- Metal GPU acceleration is **automatic** — no drivers, no CUDA, no setup
- Works on MacBook Air/Pro, Mac Mini, Mac Studio, Mac Pro (any M-series)
- The installer checks for Xcode Command Line Tools (needed to compile llama-cpp-python) and prompts to install them if missing
- **Intel Macs**: Metal is disabled automatically (llama-cpp-python shader incompatibility) — the node runs CPU-only

## Linux

```bash
curl -sSL https://llamanet.app/install.sh | sh
llamanet run hf.co/user/Model:Q4_K_M
```

- Requires Python 3.9+ and a C compiler (`sudo apt-get install build-essential` on Debian/Ubuntu)
- **NVIDIA GPUs**: install the NVIDIA driver + CUDA toolkit first — GPU layers are auto-detected
- CPU-only: `N_GPU_LAYERS=0 llamanet run hf.co/user/Model:Q4_K_M`

## Windows (10 / 11)

```powershell
irm https://llamanet.app/install.ps1 | iex
llamanet run hf.co/user/Model:Q4_K_M
```

- Python 3.9+ is auto-installed via `winget` if missing
- C++ build tools are required for llama-cpp-python — the installer checks and links to `winget install Microsoft.VisualStudio.2022.BuildTools`
- Use `start-app.bat` instead of `sh start-app.sh`
- **Note:** `start-app.bat` does not install Playwright automatically — run `pip install playwright && playwright install chromium` if you want the [computer-use agent](agent.md)

## Cloud / Remote Operators

Any always-on machine works — a cloud VPS, an old workstation, a home server:

```bash
# On the remote machine
curl -sSL https://llamanet.app/install.sh | sh
llamanet run hf.co/user/Model:Q4_K_M --tunnel
```

- `--tunnel` (default) creates a Cloudflare tunnel so the node gets a public HTTPS URL even behind NAT
- `start-app.sh` also prevents sleep while running (macOS `caffeinate`, Linux wake lock)
- Point at a private gateway instead of the public network with `--bootstrap-peers https://your-gateway` (see [Gateway & Enterprise Hosting](gateway.md))

## Cloudflare Tunnels

Every node can get a public HTTPS URL with zero router configuration.

| Mode | When | URL stability |
|---|---|---|
| **Quick tunnel** (default, no Cloudflare account) | Trying things out, one-off sharing | Random `*.trycloudflare.com` URL per run |
| **Named tunnel** (`~/.cloudflared/config.yml` present) | Production, persistent nodes | Stable custom hostname |

`start-app.sh` auto-installs `cloudflared` on macOS/Linux if missing (Homebrew or standalone binary). Windows: the installer downloads `cloudflared.exe` to `%LOCALAPPDATA%\LlamaNet\bin`.

For named tunnels, create one with `cloudflared tunnel create <name>` and write `~/.cloudflared/config.yml`; LlamaNet detects the config and uses it automatically.

## Run on a GPU Cloud Provider

LlamaNet ships a CUDA Docker image (`Dockerfile`) — bring it to any Docker GPU host.

### RunPod / vast.ai / Docker Hub

```bash
docker run --gpus all -p 8000:8000 \
  -e MODEL_URL="hf.co/mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated-GGUF:Q4_K_M" \
  -e ENABLE_TUNNEL=true \
  machaao/llamanet:latest
```

| Variable | Purpose |
|---|---|
| `MODEL_URL` | Hugging Face model to download and serve on start |
| `ENABLE_TUNNEL` | `true` (default) for a public URL, `false` for private access |
| `BOOTSTRAP_PEERS` | Gateway URL (default `https://llamanet.app`) |
| `N_GPU_LAYERS` | `-1` all layers on GPU (default), `0` CPU-only |
| `N_CTX` / `N_BATCH` / `N_UBATCH` / `N_PARALLEL` | Performance tuning (see [API Reference](api-reference.md#environment-variables)) |

On RunPod: expose port 8000 as the HTTP port; set `MODEL_URL` in the pod env. On vast.ai: pick a CUDA template, map port 8000, set the same env vars. The container health-checks `/health` and logs the tunnel URL to stdout.

### Build & Push Your Own Image

```bash
docker build -t your-registry/llamanet:latest .
docker push your-registry/llamanet:latest
```

### Operating Your Own Rig

Useful commands when running a dedicated node:

```bash
# Monitor the network
python -m tools.monitor

# Quick health check
python -m tools.quick_check

# Full network status
python -m tools.network_status
```

The node stays up across system sleep (auto re-registers with the gateway on wake). The Web UI at `localhost:8000` shows live metrics, and `/events/network` streams updates over SSE.

## See Also

- [Models & Multi-Model Pool](models.md) — what to run on your hardware
- [Gateway & Enterprise Hosting](gateway.md) — private networks and team deployments
- [API Reference](api-reference.md) — endpoints and environment variables
