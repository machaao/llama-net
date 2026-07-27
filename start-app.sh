#!/bin/sh

# LlamaNet OpenAI-Compatible Inference Node Startup Script
# This script handles deployment on MACHAAO platform and local development

set -e

# ── MACHAAO Cloud Detection ──
# When running on MACHAAO cloud, default to gateway/gateway mode
# because inference nodes need GPU and run on user machines
if [ -n "$MACHAAO_APP_ID" ]; then
    export LLAMANET_MODE="landing"
    ENABLE_TUNNEL=true
    echo "🌐 MACHAAO cloud detected — starting gateway mode"
fi

# Detect Python interpreter (prefer python3 for portability)
if [ -n "$VIRTUAL_ENV" ] && command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "❌ Python not found. Please install Python 3.8+ or activate your virtual environment."
    exit 1
fi

# ── Handle --help ──
case "${1:-}" in
    --help|-h|help)
        echo ""
        echo "  LlamaNet — Distributed AI Inference Network"
        echo "  ────────────────────────────────────────────"
        echo ""
        echo "  Usage:"
        echo "    llamanet                                  Start (no-model mode)"
        echo "    llamanet run <hf-url> [OPTIONS]           Download and run a model"
        echo ""
        echo "  Options:"
        echo "    --tunnel              Enable Cloudflare tunnel (default)"
        echo "    --no-tunnel           Disable Cloudflare tunnel"
        echo "    --bootstrap-peers URL Gateway URL (default: https://llamanet.app)"
        echo "    --port PORT           HTTP API port (default: 8000)"
        echo "    --host HOST           Bind address (default: 0.0.0.0)"
        echo "    --ctx-size N          Context window in tokens (0 = auto-detect)"
        echo "    --batch-size N        Batch size in tokens (default: 4096)"
        echo "    --ubatch-size N       Physical micro-batch size in tokens (default: 512)"
        echo "    --n-parallel N        Number of parallel slots (default: 1)"
        echo "    --threads N           CPU threads for generation (0 = auto)"
        echo "    --threads-batch N     CPU threads for prefill processing (0 = auto)"
        echo "    --flash-attn          Enable FlashAttention"
        echo "    --cache-type-k TYPE   KV cache key type: f16, q8_0, q4_0 (default: f16)"
        echo "    --cache-type-v TYPE   KV cache value type: f16, q8_0, q4_0 (default: f16)"
        echo "    --gpu-layers N        GPU layers (-1 = all)"
        echo "    --no-gpu              Disable GPU acceleration"
        echo "    --node-id ID          Custom node identifier"
        echo "    --public-ip IP        Override public IP detection"
        echo "    --verbose             Enable verbose logging"
        echo "    --help                Show this help"
        echo ""
        echo "  Examples:"
        echo "    llamanet"
        echo "    llamanet run hf.co/mistralai/Ministral-3-8B-Instruct-GGUF:Q4_K_M"
        echo "    llamanet run hf.co/user/Model:Q4_K_M --no-tunnel"
        echo "    llamanet run hf.co/user/Model:Q4_K_M --ctx-size 16384 --flash-attn"
        echo "    llamanet run hf.co/user/Model:Q4_K_M --no-gpu --cache-type-k q8_0"
        echo ""
        echo "  Web UI opens automatically at http://localhost:8000"
        echo ""
        exit 0
        ;;
esac

# ── Landing/Gateway Mode Detection ──
if [ "$LLAMANET_MODE" = "landing" ]; then
    echo "🌐 Starting llamanet.app gateway..."

    if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_SECRET_KEY" ]; then
        echo "⚠️  SUPABASE_URL or SUPABASE_SECRET_KEY not detected in shell — Python will validate at startup"
    fi

    if ! $PYTHON_CMD -c "import supabase" 2>/dev/null; then
        echo "📦 Installing Supabase client..."
        $PYTHON_CMD -m pip install supabase python-jose[cryptography]
    fi

    if ! $PYTHON_CMD -c "import landing" 2>/dev/null; then
        $PYTHON_CMD -m pip install -e .
    fi

    exec $PYTHON_CMD -m gateway.server
fi

echo "🐍 Using Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

echo "🚀 Starting LlamaNet OpenAI-Compatible Inference Node..."

# Check if we're in a containerized environment
if [ -d "/app" ] && [ "$(pwd)" = "/app" ]; then
    echo "📦 Running in containerized environment"
    CONTAINER_MODE=true
else
    echo "💻 Running in local development mode"
    CONTAINER_MODE=false
fi

# ── Cloudflare Tunnel Support ──
# Default: enable tunnel for local development (required for network participation)
if [ "$CONTAINER_MODE" = "true" ]; then
    ENABLE_TUNNEL=false
else
    ENABLE_TUNNEL=true
fi
TUNNEL_PID=""
TUNNEL_URL=""
SLEEP_PID=""

REMAINING_ARGS=""
BOOTSTRAP_PEERS_VALUE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --tunnel) ENABLE_TUNNEL=true ;;
        --no-tunnel) ENABLE_TUNNEL=false ;;
        --bootstrap-peers)
            shift
            BOOTSTRAP_PEERS_VALUE="$1"
            ;;
        run)
            # Capture 'run' and its URL — don't add to REMAINING_ARGS
            if [ -n "$2" ]; then
                HF_URL="$2"
                shift
            fi
            ;;
        *) REMAINING_ARGS="$REMAINING_ARGS $1" ;;
    esac
    shift
done
[ -n "$BOOTSTRAP_PEERS_VALUE" ] && export BOOTSTRAP_PEERS="$BOOTSTRAP_PEERS_VALUE"
[ "$ENABLE_CLOUDFLARE_TUNNEL" = "true" ] && ENABLE_TUNNEL=true
set -- $REMAINING_ARGS

if [ "$ENABLE_TUNNEL" = "true" ] && ! command -v cloudflared >/dev/null 2>&1; then
    echo "📦 Installing cloudflared..."
    INSTALL_OK=false

    if [ "$(uname)" = "Darwin" ]; then
        # macOS — try Homebrew first, then standalone binary
        if command -v brew >/dev/null 2>&1; then
            echo "   Using Homebrew..."
            if brew install cloudflared; then
                INSTALL_OK=true
            else
                echo "   ⚠️  Homebrew install failed, trying standalone binary..."
            fi
        fi

        if [ "$INSTALL_OK" = "false" ]; then
            CF_ARCH=$(uname -m)
            [ "$CF_ARCH" = "x86_64" ] && CF_ARCH="amd64"
            CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${CF_ARCH}.tgz"
            CF_TMPDIR=$(mktemp -d)
            echo "   Downloading cloudflared for macOS ${CF_ARCH}..."
            if curl -fsSL "$CF_URL" -o "$CF_TMPDIR/cloudflared.tgz" 2>/dev/null; then
                tar -xzf "$CF_TMPDIR/cloudflared.tgz" -C "$CF_TMPDIR" 2>/dev/null
                # Find the binary wherever tar extracted it
                CF_BIN=$(find "$CF_TMPDIR" -name cloudflared -type f ! -name "*.tgz" | head -1)
                if [ -n "$CF_BIN" ] && [ -f "$CF_BIN" ]; then
                    sudo install -m 755 "$CF_BIN" /usr/local/bin/cloudflared
                    INSTALL_OK=true
                    echo "   ✅ Installed to /usr/local/bin/cloudflared"
                else
                    echo "   ❌ Binary not found in downloaded archive"
                fi
            else
                echo "   ❌ Download failed. Install manually:"
                echo "      brew install cloudflared"
            fi
            rm -rf "$CF_TMPDIR"
        fi

    elif [ "$(uname)" = "Linux" ]; then
        echo "   Downloading cloudflared for Linux..."
        if curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared; then
            sudo chmod +x /usr/local/bin/cloudflared
            INSTALL_OK=true
            echo "   ✅ Installed to /usr/local/bin/cloudflared"
        else
            echo "   ❌ Download failed. Install manually:"
            echo "      curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared"
            echo "      sudo chmod +x /usr/local/bin/cloudflared"
        fi

    else
        echo "   ❌ Automatic install not supported on $(uname)"
        echo "      Download from: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/"
    fi

    if [ "$INSTALL_OK" = "false" ]; then
        echo "⚠️  Continuing without tunnel — install cloudflared and re-run with --tunnel"
        ENABLE_TUNNEL=false
    fi
fi

# Final check — verify cloudflared is available
if [ "$ENABLE_TUNNEL" = "true" ]; then
    if command -v cloudflared >/dev/null 2>&1; then
        echo "✅ cloudflared: $(cloudflared --version 2>&1 | head -1)"
    else
        echo "⚠️  cloudflared not found — continuing without tunnel"
        ENABLE_TUNNEL=false
    fi
fi

# Handle 'run' command (HF_URL captured during argument parsing above)
if [ -n "$HF_URL" ]; then
    : # HF_URL already captured
else
    # Set default values for non-run commands
    DEFAULT_MODEL_PATH="${MODEL_PATH:-./models/model.gguf}"
fi
DEFAULT_HOST="${HOST:-0.0.0.0}"
DEFAULT_PORT="${PORT:-8000}"
DEFAULT_NODE_ID="${NODE_ID:-}"
DEFAULT_BOOTSTRAP_PEERS="${BOOTSTRAP_PEERS:-${BOOTSTRAP_NODES:-}}"
# Default to public LlamaNet gateway when no peers specified
if [ -z "$DEFAULT_BOOTSTRAP_PEERS" ] && [ -z "$MACHAAO_APP_ID" ]; then
    DEFAULT_BOOTSTRAP_PEERS="https://llamanet.app"
fi
DEFAULT_PUBLIC_IP="${PUBLIC_IP:-}"

# ── Intel Mac Metal Compatibility ──
# Auto-detect Intel Macs and disable Metal to prevent shader compilation errors
# NOTE: uname -m returns x86_64 under Rosetta 2 on Apple Silicon, so we must
# distinguish real Intel hardware from Rosetta-translated processes.
if [ "$(uname)" = "Darwin" ]; then
    IS_INTEL_MAC=false
    if [ "$(uname -m)" = "x86_64" ]; then
        # Could be real Intel OR Rosetta 2 on Apple Silicon — check hardware
        ROSETTA_TRANSLATED=$(/usr/sbin/sysctl -n sysctl.proc_translated 2>/dev/null || echo "0")
        if [ "$ROSETTA_TRANSLATED" = "1" ]; then
            echo "✅ Apple Silicon detected (running under Rosetta 2) — Metal GPU enabled"
        else
            IS_INTEL_MAC=true
        fi
    fi

    if [ "$IS_INTEL_MAC" = "true" ] && [ -z "$LLAMA_NO_METAL" ]; then
        export LLAMA_NO_METAL=1
        echo "⚠️  Intel Mac detected — setting LLAMA_NO_METAL=1 (CPU-only mode)"
        echo "   Metal shaders in llama-cpp-python 0.3.x are incompatible with Intel Macs"
        echo "   Set LLAMA_NO_METAL=0 to override (may fail)"
    fi
fi

# Suppress Python semaphore warnings for cleaner output
export PYTHONWARNINGS="ignore:semaphore:UserWarning:multiprocessing.resource_tracker,ignore:resource_tracker"
export PYTHONDONTWRITEBYTECODE=1

# Validate model file exists (or enter no-model mode)
if [ -n "$HF_URL" ]; then
    echo "📦 Model will be downloaded: $HF_URL"
elif [ -z "$DEFAULT_MODEL_PATH" ] || [ ! -f "$DEFAULT_MODEL_PATH" ]; then
    echo "🌐 Starting in no-model mode — download a model via Web UI"
    DEFAULT_MODEL_PATH=""
else
    echo "✅ Model file found: $DEFAULT_MODEL_PATH"
fi

# Check if Python dependencies are installed
if [ "$LLAMANET_MODE" = "landing" ]; then
    # Gateway mode: only need lightweight dependencies
    if ! $PYTHON_CMD -c "import fastapi, uvicorn" 2>/dev/null; then
        echo "📦 Installing gateway dependencies..."
        if [ -f "requirements.txt" ]; then
            $PYTHON_CMD -m pip install -r requirements.txt
        else
            echo "❌ Error: requirements.txt not found"
            exit 1
        fi
    fi
else
    # Inference mode: need full dependencies including llama-cpp-python
    if ! $PYTHON_CMD -c "import fastapi, uvicorn, llama_cpp" 2>/dev/null; then
        echo "📦 Installing inference dependencies..."
        if [ -f "requirements-inference.txt" ]; then
            $PYTHON_CMD -m pip install -r requirements-inference.txt
        elif [ -f "requirements.txt" ]; then
            $PYTHON_CMD -m pip install -r requirements.txt
        else
            echo "❌ Error: requirements.txt not found"
            exit 1
        fi
    fi
fi

# Install package in development mode if not already installed
if ! $PYTHON_CMD -c "import inference_node" 2>/dev/null; then
    echo "📦 Installing LlamaNet package..."
    $PYTHON_CMD -m pip install -e .
fi

start_cloudflare_tunnel() {
    local port=$1
    local tunnel_log="/tmp/llamanet_tunnel_$$.log"
    local config_file="$HOME/.cloudflared/config.yml"

    # Remove stale tunnel URL from previous runs
    rm -f "${TMPDIR:-/tmp}/llamanet_tunnel_url"

    echo ""

    # ── Named tunnel with existing config.yml ──
    if [ -f "$config_file" ]; then
        EXISTING_TUNNEL=$(grep '^tunnel:' "$config_file" 2>/dev/null | awk '{print $2}')

        if [ -z "$EXISTING_TUNNEL" ]; then
            echo "❌ No 'tunnel:' field found in $config_file"
            exit 1
        fi

        # Extract hostname from ingress
        EXISTING_DOMAIN=$(grep 'hostname:' "$config_file" 2>/dev/null | head -1 | awk -F': ' '{print $2}')

        echo "🌐 Starting Cloudflare tunnel: $EXISTING_TUNNEL"
        [ -n "$EXISTING_DOMAIN" ] && echo "   Domain: $EXISTING_DOMAIN"
        echo "   Config: $config_file"
        echo "   Log: $tunnel_log"

        # Pre-flight: verify credentials file exists
        CRED_FILE=$(grep 'credentials-file:' "$config_file" 2>/dev/null | awk '{print $2}')
        # Expand ~ if present
        CRED_FILE_EXPANDED=$(eval echo "$CRED_FILE")
        if [ -n "$CRED_FILE_EXPANDED" ] && [ ! -f "$CRED_FILE_EXPANDED" ]; then
            echo "❌ Credentials file not found: $CRED_FILE_EXPANDED"
            echo "   Check 'credentials-file' in $config_file"
            echo "   Run: ls -la ~/.cloudflared/*.json"
            return
        fi

        cloudflared tunnel --config "$config_file" run > "$tunnel_log" 2>&1 &
        TUNNEL_PID=$!

        # Wait for tunnel to connect — use strict error matching
        local attempts=0
        local connected=false
        while [ $attempts -lt 30 ]; do
            # Check if cloudflared is still running
            if ! kill -0 $TUNNEL_PID 2>/dev/null; then
                echo ""
                echo "❌ cloudflared exited unexpectedly (exit code: $?). Log:"
                cat "$tunnel_log"
                break
            fi

            # Success indicators
            if grep -q "Registered" "$tunnel_log" 2>/dev/null || \
               grep -q "connIndex" "$tunnel_log" 2>/dev/null; then
                connected=true
                break
            fi

            # Only match FATAL/ERR level errors (not info messages containing "error")
            if grep -qE "^.*(ERR |FTL |error failed to serve|couldn.t connect)" "$tunnel_log" 2>/dev/null; then
                echo ""
                echo "❌ cloudflared fatal error detected:"
                tail -10 "$tunnel_log"
                break
            fi

            sleep 1
            attempts=$((attempts + 1))
        done

        # Only show success if actually connected
        if [ "$connected" = "true" ] && [ -n "$EXISTING_DOMAIN" ]; then
            TUNNEL_URL="https://${EXISTING_DOMAIN}"
        elif [ "$connected" = "false" ]; then
            echo "⚠️  Tunnel may not have connected after ${attempts}s."
            echo "   Log output:"
            tail -20 "$tunnel_log"
            echo ""
            echo "   Troubleshooting:"
            echo "   1. Verify credentials: ls -la $CRED_FILE_EXPANDED"
            echo "   2. Verify tunnel exists: cloudflared tunnel list"
            echo "   3. Verify DNS: dig $EXISTING_DOMAIN"
            echo "   4. Try manually: cloudflared tunnel --config $config_file run"
        fi

    # ── Quick temporary tunnel (no config.yml) ──
    else
        echo "🌐 Starting quick Cloudflare tunnel on port $port"
        cloudflared tunnel --url "http://localhost:$port" > "$tunnel_log" 2>&1 &
        TUNNEL_PID=$!

        local attempts=0
        while [ $attempts -lt 30 ]; do
            TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$tunnel_log" 2>/dev/null | head -1)
            [ -n "$TUNNEL_URL" ] && break
            sleep 0.5
            attempts=$((attempts + 1))
        done
    fi

    if [ -n "$TUNNEL_URL" ]; then
        export LLAMANET_TUNNEL_URL="$TUNNEL_URL"
        # Write tunnel URL to file so the Python process can discover it
        echo "$TUNNEL_URL" > "${TMPDIR:-/tmp}/llamanet_tunnel_url"
        echo ""
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║  🌍 Cloudflare Tunnel Active                                ║"
        echo "║                                                              ║"
        echo "║  Public URL: $TUNNEL_URL"
        echo "║                                                              ║"
        echo "║  Share this URL to access LlamaNet from anywhere.            ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        echo ""
    else
        echo "⚠️  Tunnel starting... URL will appear in logs: $tunnel_log"
    fi
}

# Health check endpoint
health_check() {
    local port=$1
    local max_attempts=30
    local attempt=1
    
    echo "🔍 Waiting for service to be ready on port $port..."
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://localhost:$port/health" >/dev/null 2>&1; then
            echo "✅ Service is ready!"
            return 0
        fi
        
        echo "⏳ Attempt $attempt/$max_attempts - waiting for service..."
        sleep 2
        attempt=$((attempt + 1))
    done
    
    echo "❌ Service failed to start within expected time"
    return 1
}

# ── Sleep Prevention ──
# Prevents the OS from sleeping while running a dedicated node (--tunnel mode).
# Only called when --tunnel flag is active (operator intends persistent uptime).

prevent_sleep() {
    OS="$(uname)"
    case "$OS" in
        Darwin)
            if command -v caffeinate >/dev/null 2>&1; then
                caffeinate -s -i &
                SLEEP_PID=$!
                echo "✅ macOS sleep prevention active (caffeinate PID: $SLEEP_PID)"
            else
                echo "⚠️  caffeinate not found — system may sleep during idle periods"
            fi
            ;;
        Linux)
            if [ -w /sys/power/wake_lock ] 2>/dev/null; then
                echo "llamanet-node" > /sys/power/wake_lock 2>/dev/null && \
                    echo "✅ Linux wake lock acquired" || \
                    echo "⚠️  Could not acquire wake lock (try running as root)"
            else
                echo "⚠️  Cannot write to /sys/power/wake_lock"
                echo "   To prevent sleep manually: sudo systemctl mask sleep.target suspend.target"
            fi
            ;;
        *)
            echo "⚠️  Automatic sleep prevention not supported on $OS"
            echo "   Please disable sleep manually in your power settings"
            ;;
    esac
}

cleanup_sleep() {
    OS="$(uname)"
    case "$OS" in
        Darwin)
            if [ -n "$SLEEP_PID" ]; then
                kill $SLEEP_PID 2>/dev/null
                echo "✅ Stopped caffeinate"
            fi
            ;;
        Linux)
            if [ -w /sys/power/wake_unlock ] 2>/dev/null; then
                echo "llamanet-node" > /sys/power/wake_unlock 2>/dev/null
                echo "✅ Released wake lock"
            fi
            ;;
    esac
}

# Signal handler for graceful shutdown
cleanup() {
    echo "🛑 Received shutdown signal, stopping LlamaNet node..."
    cleanup_sleep

    # Stop server FIRST — it sends departure event to gateway before shutting down
    if [ ! -z "$SERVER_PID" ]; then
        echo "📤 Sending SIGTERM to server process $SERVER_PID..."
        # Send SIGTERM and let the application handle graceful shutdown
        kill -TERM $SERVER_PID 2>/dev/null || true

        # Wait for graceful shutdown with appropriate timeout
        echo "⏳ Waiting for graceful shutdown (max 10 seconds)..."
        for i in $(seq 1 10); do
            if ! kill -0 $SERVER_PID 2>/dev/null; then
                echo "✅ Server shut down gracefully"
                exit 0
            fi
            sleep 1
        done
        
        # Send SIGINT if still running
        echo "⚠️ Sending SIGINT for faster shutdown..."
        kill -INT $SERVER_PID 2>/dev/null || true
        
        # Wait a bit more
        for i in $(seq 1 3); do
            if ! kill -0 $SERVER_PID 2>/dev/null; then
                echo "✅ Server shut down after SIGINT"
                exit 0
            fi
            sleep 1
        done
        
        # Force kill if still running
        echo "⚠️ Forcing server shutdown..."
        kill -KILL $SERVER_PID 2>/dev/null || true
    fi

    # Now stop tunnel AFTER server has finished
    if [ ! -z "$TUNNEL_PID" ]; then
        echo "🌐 Stopping Cloudflare Tunnel..."
        kill $TUNNEL_PID 2>/dev/null || true
    fi
    exit 0
}

# Set up signal traps - only trap in shell script, not in Python
trap cleanup INT TERM

# Build command line arguments
if [ -n "$DEFAULT_MODEL_PATH" ]; then
    ARGS="--model-path $DEFAULT_MODEL_PATH"
else
    ARGS=""
fi
ARGS="$ARGS --host $DEFAULT_HOST"
ARGS="$ARGS --port $DEFAULT_PORT"

if [ -n "$DEFAULT_NODE_ID" ]; then
    ARGS="$ARGS --node-id $DEFAULT_NODE_ID"
fi


if [ -n "$DEFAULT_PUBLIC_IP" ]; then
    ARGS="$ARGS --public-ip $DEFAULT_PUBLIC_IP"
fi

if [ -n "$DEFAULT_BOOTSTRAP_PEERS" ]; then
    ARGS="$ARGS --bootstrap-peers $DEFAULT_BOOTSTRAP_PEERS"
fi

# Append passthrough flags (--ctx-size, --no-gpu, --gpu-layers, etc.)
if [ -n "$REMAINING_ARGS" ]; then
    ARGS="$ARGS $REMAINING_ARGS"
fi

echo "🔧 Configuration:"
if [ -n "$HF_URL" ]; then
    echo "   Model: $HF_URL (will download)"
elif [ -n "$DEFAULT_MODEL_PATH" ]; then
    echo "   Model: $DEFAULT_MODEL_PATH"
else
    echo "   Model: (none - download via Web UI)"
fi
echo "   Host: $DEFAULT_HOST"
echo "   HTTP Port: $DEFAULT_PORT"
echo "   Node ID: ${DEFAULT_NODE_ID:-auto-generated}"
echo "   Bootstrap Peers: ${DEFAULT_BOOTSTRAP_PEERS:-none}"
echo "   Public IP: ${DEFAULT_PUBLIC_IP:-auto-detect}"

# Start the inference node
echo "🚀 Starting inference node with OpenAI-compatible API..."
echo "📡 API will be available at: http://$DEFAULT_HOST:$DEFAULT_PORT"
echo "🌐 Web UI will be available at: http://$DEFAULT_HOST:$DEFAULT_PORT"
echo "🔗 OpenAI-compatible endpoints:"
echo "   - GET  /v1/models"
echo "   - POST /v1/completions"
echo "   - POST /v1/chat/completions"

# Start the server in background for health check
if [ -n "$HF_URL" ]; then
    $PYTHON_CMD -m inference_node.server run "$HF_URL" $ARGS &
else
    $PYTHON_CMD -m inference_node.server $ARGS &
fi
SERVER_PID=$!

# Wait for service to be ready
if health_check $DEFAULT_PORT; then
    echo "🎉 LlamaNet OpenAI-Compatible Inference Node is running!"
    
    if [ "$ENABLE_TUNNEL" = "true" ]; then
        start_cloudflare_tunnel $DEFAULT_PORT
        prevent_sleep
    fi

    # Open browser on local development (not container, not MACHAAO cloud)
    if [ "$CONTAINER_MODE" = "false" ] && [ -z "$MACHAAO_APP_ID" ]; then
        if [ "$(uname)" = "Darwin" ]; then
            open "http://localhost:$DEFAULT_PORT" 2>/dev/null &
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "http://localhost:$DEFAULT_PORT" 2>/dev/null &
        fi
    fi

    echo ""
    echo "🌐 Web UI: http://localhost:$DEFAULT_PORT"
    echo "📊 Monitor network status: python -m tools.monitor"
    echo "🔍 Quick network check: python -m tools.quick_check"
    echo "🛑 Press Ctrl+C for graceful shutdown"
    
    # Keep the server running in foreground with proper signal handling
    wait $SERVER_PID
    exit_code=$?
    
    if [ $exit_code -eq 0 ]; then
        echo "✅ Server exited gracefully"
    else
        echo "❌ Server exited with code $exit_code"
    fi
    
    exit $exit_code
else
    echo "❌ Failed to start service"
    kill $SERVER_PID 2>/dev/null || true
    exit 1
fi
