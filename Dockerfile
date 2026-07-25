# ═══════════════════════════════════════════════════════════════
# LlamaNet — Docker Image for GPU Cloud Providers
# Supports: Docker Hub, RunPod, vast.ai, any Docker host
#
# Build:
#   docker build -t machaao/llamanet:latest .
#
# Run:
#   docker run --gpus all -p 8000:8000 \
#     -e MODEL_URL="hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M" \
#     machaao/llamanet:latest
# ═══════════════════════════════════════════════════════════════

FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive

# ── System Dependencies ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3-pip \
    git \
    build-essential \
    cmake \
    curl \
    ca-certificates \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && rm -rf /var/lib/apt/lists/*

# ── Install cloudflared for tunnel support ──
RUN curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

# ── Upgrade pip ──
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ── Set working directory ──
WORKDIR /app

# ── Copy dependency files first (Docker layer caching) ──
COPY requirements.txt requirements-inference.txt setup.py pyproject.toml ./

# ── Install gateway dependencies ──
RUN pip install --no-cache-dir -r requirements.txt

# ── Install llama-cpp-python with CUDA support ──
# Using pre-built CUDA 12.1 wheel from abetlen's index
RUN pip install --no-cache-dir \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 \
    llama-cpp-python==0.3.34

# ── Install remaining inference dependencies ──
RUN pip install --no-cache-dir -r requirements-inference.txt

# ── Copy source code ──
COPY . .

# ── Install LlamaNet package ──
RUN pip install --no-cache-dir -e .

# ── Make scripts executable ──
RUN chmod +x start-app.sh scripts/*.sh 2>/dev/null || true

# ── Create model cache directory ──
RUN mkdir -p /root/.llamanet

# ── Environment defaults ──
ENV HOST=0.0.0.0
ENV PORT=8000
ENV N_GPU_LAYERS=-1
ENV N_CTX=4096
ENV BOOTSTRAP_PEERS=https://llamanet.app
ENV PYTHONUNBUFFERED=1
ENV PYTHONWARNINGS="ignore:semaphore:UserWarning:multiprocessing.resource_tracker"

# ── Expose port ──
EXPOSE 8000

# ── Health check ──
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# ── Entrypoint ──
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
