# Multi-stage Dockerfile for LlamaNet with GPU/CPU auto-detection
ARG PYTHON_VERSION=3.11
ARG CUDA_VERSION=12.1.1
ARG UBUNTU_VERSION=22.04

# Base stage - common dependencies
FROM python:${PYTHON_VERSION}-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    wget \
    curl \
    pkg-config \
    libssl-dev \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt requirements-dev.txt ./

# GPU detection and setup stage
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} as gpu-base

# Install Python
RUN apt-get update && apt-get install -y \
    python${PYTHON_VERSION} \
    python3-pip \
    python3-dev \
    build-essential \
    cmake \
    git \
    wget \
    curl \
    pkg-config \
    libssl-dev \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Create symlinks for python
RUN ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3

WORKDIR /app

# Copy requirements
COPY requirements.txt requirements-dev.txt ./

# Final runtime stage
FROM base as runtime

# Copy detection and startup scripts
COPY docker/gpu-detect.sh /usr/local/bin/gpu-detect.sh
COPY docker/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/gpu-detect.sh /usr/local/bin/start.sh

# Install base Python dependencies (without llama-cpp-python)
RUN pip install --no-cache-dir --upgrade pip && \
    grep -v "llama-cpp-python" requirements.txt > /tmp/requirements-base.txt && \
    pip install --no-cache-dir -r /tmp/requirements-base.txt

# Copy application code
COPY . .

# Install package in development mode
RUN pip install -e .

# Create directory for models
RUN mkdir -p /models

# Environment variables for configuration
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV MODEL_PATH=/models/model.gguf
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DHT_PORT=8001
ENV BOOTSTRAP_NODES=""
ENV HARDWARE_MODE=auto
ENV N_GPU_LAYERS=0

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Expose ports
EXPOSE 8000 8001

# Use startup script as entrypoint
ENTRYPOINT ["/usr/local/bin/start.sh"]
CMD ["python", "-m", "inference_node.server"]
