#!/bin/sh
# ═══════════════════════════════════════════════════════════════
# LlamaNet Docker Entrypoint
# Works with: Docker Hub, RunPod, vast.ai, plain Docker
#
# Environment Variables:
#   MODEL_URL       HuggingFace model URL (e.g. hf.co/user/Model:Q4_K_M)
#   ENABLE_TUNNEL   Enable Cloudflare tunnel (default: true)
#   N_GPU_LAYERS    GPU layers (-1 = all, 0 = CPU only) (default: -1)
#   N_CTX           Context window in tokens (default: 4096)
#   N_BATCH         Batch size in tokens (default: 4096)
#   BOOTSTRAP_PEERS Gateway URL (default: https://llamanet.app)
#   PORT            HTTP API port (default: 8000)
# ═══════════════════════════════════════════════════════════════

set -e

echo ""
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║  🦙 LlamaNet — GPU Cloud Node                       ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo ""

# ── Detect GPU ──
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "✅ NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "   (details unavailable)"
else
    echo "⚠️  No nvidia-smi found — GPU may not be available"
    if [ -z "$N_GPU_LAYERS" ] || [ "$N_GPU_LAYERS" = "-1" ]; then
        echo "   Setting N_GPU_LAYERS=0 (CPU-only mode)"
        export N_GPU_LAYERS=0
    fi
fi

# ── Set defaults ──
export ENABLE_TUNNEL="${ENABLE_TUNNEL:-true}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"

# ── Build start-app.sh arguments ──
ARGS=""

# Add tunnel flag
if [ "$ENABLE_TUNNEL" = "true" ]; then
    ARGS="$ARGS --tunnel"
else
    ARGS="$ARGS --no-tunnel"
fi

# Add bootstrap peers
if [ -n "$BOOTSTRAP_PEERS" ]; then
    ARGS="$ARGS --bootstrap-peers $BOOTSTRAP_PEERS"
fi

# ── Handle model URL ──
if [ -n "$MODEL_URL" ]; then
    echo ""
    echo "📦 Model: $MODEL_URL"
    echo "🔧 GPU Layers: ${N_GPU_LAYERS:-all}"
    echo "📏 Context: ${N_CTX:-4096} tokens"
    echo "🌐 Tunnel: ${ENABLE_TUNNEL}"
    echo "📡 Gateway: ${BOOTSTRAP_PEERS:-https://llamanet.app}"
    echo ""

    # Use start-app.sh run <model-url> with additional args
    exec sh start-app.sh run "$MODEL_URL" $ARGS
else
    echo ""
    echo "🌐 No MODEL_URL set — starting in no-model mode"
    echo "   Download a model via Web UI at http://localhost:${PORT}"
    echo ""
    echo "💡 To pre-load a model, set:"
    echo "   MODEL_URL=hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M"
    echo ""

    # No-model mode: start without a model
    exec sh start-app.sh $ARGS
fi
