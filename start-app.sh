#!/bin/sh

# LlamaNet OpenAI-Compatible Inference Node Startup Script
# This script handles deployment on MACHAAO platform and local development

set -e

# ── MACHAAO Cloud Detection ──
# When running on MACHAAO cloud, default to landing/gateway mode
# because inference nodes need GPU and run on user machines
if [ -n "$MACHAAO_APP_ID" ]; then
    export LLAMANET_MODE="landing"
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

    exec $PYTHON_CMD -m landing.server
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
ENABLE_TUNNEL=false
TUNNEL_PID=""
TUNNEL_URL=""

REMAINING_ARGS=""
for arg in "$@"; do
    case "$arg" in
        --tunnel) ENABLE_TUNNEL=true ;;
        *) REMAINING_ARGS="$REMAINING_ARGS $arg" ;;
    esac
done
[ "$ENABLE_CLOUDFLARE_TUNNEL" = "true" ] && ENABLE_TUNNEL=true
set -- $REMAINING_ARGS

if [ "$ENABLE_TUNNEL" = "true" ] && ! command -v cloudflared >/dev/null 2>&1; then
    echo "📦 Installing cloudflared..."
    if [ "$(uname)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            brew install cloudflared
        else
            CF_ARCH=$(uname -m); [ "$CF_ARCH" = "arm64" ] && CF_ARCH="arm64" || CF_ARCH="amd64"
            curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${CF_ARCH}.tgz" -o /tmp/cf.tgz
            tar -xzf /tmp/cf.tgz -C /tmp && sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
            rm -f /tmp/cf.tgz /tmp/cloudflared
        fi
    elif [ "$(uname)" = "Linux" ]; then
        curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
        sudo chmod +x /usr/local/bin/cloudflared
    else
        echo "❌ Install cloudflared manually"; ENABLE_TUNNEL=false
    fi
fi
[ "$ENABLE_TUNNEL" = "true" ] && command -v cloudflared >/dev/null 2>&1 && echo "✅ cloudflared: $(cloudflared --version 2>&1 | head -1)" || { ENABLE_TUNNEL=false; }

# Handle 'run' command
if [ "$1" = "run" ]; then
    if [ -z "$2" ]; then
        echo "❌ Usage: llamanet run <huggingface-url>"
        echo "   Example: llamanet run hf.co/meta-llama/Llama-2-7b-chat-hf:Q4_K_M"
        exit 1
    fi
    
    HF_URL="$2"
    shift 2  # Remove 'run' and URL from arguments
    
    echo "🔗 Downloading model from Hugging Face: $HF_URL"
    
    # Create models directory if it doesn't exist
    MODELS_DIR="${HOME}/.llamanet/models"
    mkdir -p "$MODELS_DIR"
    
    # Run the Python model downloader
    $PYTHON_CMD -c "
from inference_node.model_manager import ModelManager
import sys

manager = ModelManager()
try:
    model_path = manager.resolve_model_path('$HF_URL')
    print(f'MODEL_PATH={model_path}')
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
" > /tmp/llamanet_model_path.txt 2>&1
    
    if [ $? -ne 0 ]; then
        echo "❌ Failed to download model"
        cat /tmp/llamanet_model_path.txt
        rm -f /tmp/llamanet_model_path.txt
        exit 1
    fi
    
    MODEL_PATH=$(grep "MODEL_PATH=" /tmp/llamanet_model_path.txt | cut -d'=' -f2)
    rm -f /tmp/llamanet_model_path.txt
    
    if [ -z "$MODEL_PATH" ] || [ ! -f "$MODEL_PATH" ]; then
        echo "❌ Model file not found after download"
        exit 1
    fi
    
    echo "✅ Model downloaded to: $MODEL_PATH"
    
    # Set the model path for the inference node
    export MODEL_PATH="$MODEL_PATH"
    DEFAULT_MODEL_PATH="$MODEL_PATH"
else
    # Set default values for non-run commands
    DEFAULT_MODEL_PATH="${MODEL_PATH:-./models/model.gguf}"
fi
DEFAULT_HOST="${HOST:-0.0.0.0}"
DEFAULT_PORT="${PORT:-8000}"
DEFAULT_DHT_PORT="${DHT_PORT:-8001}"
DEFAULT_NODE_ID="${NODE_ID:-}"
DEFAULT_BOOTSTRAP_NODES="${BOOTSTRAP_NODES:-}"
DEFAULT_PUBLIC_IP="${PUBLIC_IP:-}"

# Suppress Python semaphore warnings for cleaner output
export PYTHONWARNINGS="ignore:semaphore:UserWarning:multiprocessing.resource_tracker,ignore:resource_tracker"
export PYTHONDONTWRITEBYTECODE=1

# Validate model file exists (or enter no-model mode)
if [ ! -f "$DEFAULT_MODEL_PATH" ]; then
    echo "⚠️  No model file found at $DEFAULT_MODEL_PATH"
    echo "🌐 Starting in no-model mode - use the Web UI to download a model"
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

# Signal handler for graceful shutdown
cleanup() {
    echo "🛑 Received shutdown signal, stopping LlamaNet node..."
    if [ ! -z "$TUNNEL_PID" ]; then
        echo "🌐 Stopping Cloudflare Tunnel..."
        kill $TUNNEL_PID 2>/dev/null || true
    fi

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
    exit 0
}

# Set up signal traps - only trap in shell script, not in Python
trap cleanup SIGINT SIGTERM

# Build command line arguments
if [ -n "$DEFAULT_MODEL_PATH" ]; then
    ARGS="--model-path $DEFAULT_MODEL_PATH"
else
    ARGS=""
fi
ARGS="$ARGS --host $DEFAULT_HOST"
ARGS="$ARGS --port $DEFAULT_PORT"
ARGS="$ARGS --dht-port $DEFAULT_DHT_PORT"

if [ -n "$DEFAULT_NODE_ID" ]; then
    ARGS="$ARGS --node-id $DEFAULT_NODE_ID"
fi

if [ -n "$DEFAULT_BOOTSTRAP_NODES" ]; then
    ARGS="$ARGS --bootstrap-nodes $DEFAULT_BOOTSTRAP_NODES"
fi

if [ -n "$DEFAULT_PUBLIC_IP" ]; then
    ARGS="$ARGS --public-ip $DEFAULT_PUBLIC_IP"
fi

echo "🔧 Configuration:"
if [ -n "$DEFAULT_MODEL_PATH" ]; then
    echo "   Model: $DEFAULT_MODEL_PATH"
else
    echo "   Model: (none - download via Web UI)"
fi
echo "   Host: $DEFAULT_HOST"
echo "   HTTP Port: $DEFAULT_PORT"
echo "   DHT Port: $DEFAULT_DHT_PORT"
echo "   Node ID: ${DEFAULT_NODE_ID:-auto-generated}"
echo "   Bootstrap Nodes: ${DEFAULT_BOOTSTRAP_NODES:-none (bootstrap mode)}"
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
$PYTHON_CMD -m inference_node.server $ARGS &
SERVER_PID=$!

# Wait for service to be ready
if health_check $DEFAULT_PORT; then
    echo "🎉 LlamaNet OpenAI-Compatible Inference Node is running!"
    
    if [ "$ENABLE_TUNNEL" = "true" ]; then
        start_cloudflare_tunnel $DEFAULT_PORT
    fi

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
