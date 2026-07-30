#!/bin/sh
# ═══════════════════════════════════════════════════════════════
# LlamaNet Docker Entrypoint
# Works with: Docker Hub, RunPod, vast.ai, plain Docker
#
# Environment Variables:
#   MODEL_URL       HuggingFace model URL (e.g. hf.co/user/Model:Q4_K_M)
#   ENABLE_TUNNEL   Enable Cloudflare tunnel (default: true)
#   N_GPU_LAYERS    GPU layers (-1 = all, 0 = CPU only) (default: -1)
#   N_CTX           Context window in tokens (0 = auto-detect, default: 0)
#   N_BATCH         Batch size in tokens (default: 4096)
#   N_UBATCH        Physical micro-batch size in tokens (default: 512)
#   N_PARALLEL      Number of parallel slots (default: 1)
#   N_THREADS       CPU threads for generation (0 = auto)
#   N_THREADS_BATCH CPU threads for prefill (0 = auto)
#   FLASH_ATTN      Enable FlashAttention (true/false, default: false)
#   CACHE_TYPE_K    KV cache key type: f16, q8_0, q4_0 (default: f16)
#   CACHE_TYPE_V    KV cache value type: f16, q8_0, q4_0 (default: f16)
#   MAX_MODELS      Max models in pool (0 = auto-detect from RAM, default: 0)
#   MEMORY_BUDGET_GB  Max RAM for models in GB (0 = auto-detect, default: 0)
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
export N_CTX="${N_CTX:-0}"
export N_BATCH="${N_BATCH:-256}"
export N_UBATCH="${N_UBATCH:-512}"
export N_PARALLEL="${N_PARALLEL:-1}"
export N_THREADS="${N_THREADS:-0}"
export N_THREADS_BATCH="${N_THREADS_BATCH:-0}"
export FLASH_ATTN="${FLASH_ATTN:-false}"
export CACHE_TYPE_K="${CACHE_TYPE_K:-f16}"
export CACHE_TYPE_V="${CACHE_TYPE_V:-f16}"
export MAX_MODELS="${MAX_MODELS:-0}"
export MEMORY_BUDGET_GB="${MEMORY_BUDGET_GB:-0}"

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

# Pass through all tuning flags
[ "$N_CTX" != "0" ] && ARGS="$ARGS --ctx-size $N_CTX"
[ "$N_BATCH" != "256" ] && ARGS="$ARGS --batch-size $N_BATCH"
[ "$N_UBATCH" != "512" ] && ARGS="$ARGS --ubatch-size $N_UBATCH"
[ "$N_PARALLEL" != "1" ] && ARGS="$ARGS --n-parallel $N_PARALLEL"
[ "$N_THREADS" != "0" ] && ARGS="$ARGS --threads $N_THREADS"
[ "$N_THREADS_BATCH" != "0" ] && ARGS="$ARGS --threads-batch $N_THREADS_BATCH"
[ "$N_GPU_LAYERS" != "-1" ] && [ "$N_GPU_LAYERS" != "0" ] && ARGS="$ARGS --gpu-layers $N_GPU_LAYERS"
[ "$N_GPU_LAYERS" = "0" ] && ARGS="$ARGS --no-gpu"
[ "$FLASH_ATTN" = "true" ] && ARGS="$ARGS --flash-attn"
[ "$CACHE_TYPE_K" != "f16" ] && ARGS="$ARGS --cache-type-k $CACHE_TYPE_K"
[ "$CACHE_TYPE_V" != "f16" ] && ARGS="$ARGS --cache-type-v $CACHE_TYPE_V"
[ -n "$MAX_MODELS" ] && [ "$MAX_MODELS" != "0" ] && ARGS="$ARGS --max-models $MAX_MODELS"
[ -n "$MEMORY_BUDGET_GB" ] && [ "$MEMORY_BUDGET_GB" != "0" ] && ARGS="$ARGS --memory-budget-gb $MEMORY_BUDGET_GB"

# ── Handle model URL ──
if [ -n "$MODEL_URL" ]; then
    echo ""
    echo "📦 Model: $MODEL_URL"
    echo "🔧 GPU Layers: ${N_GPU_LAYERS:-all}"
    echo "📏 Context: ${N_CTX:-auto} tokens"
    echo "📦 Batch: ${N_BATCH:-256} | µBatch: ${N_UBATCH:-512}"
    echo "⚡ Parallel slots: ${N_PARALLEL:-1}"
    echo "🧠 FlashAttn: ${FLASH_ATTN:-false}"
    echo "💾 KV Cache: K=${CACHE_TYPE_K:-f16} V=${CACHE_TYPE_V:-f16}"
    echo "🏊 Pool: max=${MAX_MODELS:-0} models, budget=${MEMORY_BUDGET_GB:-0} GB"
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
