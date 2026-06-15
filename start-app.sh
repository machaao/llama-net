#!/bin/sh

# LlamaNet OpenAI-Compatible Inference Node Startup Script
# This script handles deployment on MACHAAO platform and local development

set -e

echo "🚀 Starting LlamaNet OpenAI-Compatible Inference Node..."

# Check if we're in a containerized environment
if [ -d "/app" ] && [ "$(pwd)" = "/app" ]; then
    echo "📦 Running in containerized environment"
    CONTAINER_MODE=true
else
    echo "💻 Running in local development mode"
    CONTAINER_MODE=false
fi

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
    python -c "
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

# Suppress Python semaphore warnings for cleaner output
export PYTHONWARNINGS="ignore:semaphore:UserWarning:multiprocessing.resource_tracker,ignore:resource_tracker"
export PYTHONDONTWRITEBYTECODE=1

# Validate model file exists
if [ ! -f "$DEFAULT_MODEL_PATH" ]; then
    echo "❌ Error: Model file not found at $DEFAULT_MODEL_PATH"
    echo "Please set MODEL_PATH environment variable or place model at ./models/model.gguf"
    exit 1
fi

echo "✅ Model file found: $DEFAULT_MODEL_PATH"

# Check if Python dependencies are installed
if ! python -c "import fastapi, uvicorn, llama_cpp" 2>/dev/null; then
    echo "📦 Installing Python dependencies..."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
    else
        echo "❌ Error: requirements.txt not found"
        exit 1
    fi
fi

# Install package in development mode if not already installed
if ! python -c "import inference_node" 2>/dev/null; then
    echo "📦 Installing LlamaNet package..."
    pip install -e .
fi

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
ARGS="--model-path $DEFAULT_MODEL_PATH"
ARGS="$ARGS --host $DEFAULT_HOST"
ARGS="$ARGS --port $DEFAULT_PORT"
ARGS="$ARGS --dht-port $DEFAULT_DHT_PORT"

if [ -n "$DEFAULT_NODE_ID" ]; then
    ARGS="$ARGS --node-id $DEFAULT_NODE_ID"
fi

if [ -n "$DEFAULT_BOOTSTRAP_NODES" ]; then
    ARGS="$ARGS --bootstrap-nodes $DEFAULT_BOOTSTRAP_NODES"
fi

echo "🔧 Configuration:"
echo "   Model: $DEFAULT_MODEL_PATH"
echo "   Host: $DEFAULT_HOST"
echo "   HTTP Port: $DEFAULT_PORT"
echo "   DHT Port: $DEFAULT_DHT_PORT"
echo "   Node ID: ${DEFAULT_NODE_ID:-auto-generated}"
echo "   Bootstrap Nodes: ${DEFAULT_BOOTSTRAP_NODES:-none (bootstrap mode)}"

# Start the inference node
echo "🚀 Starting inference node with OpenAI-compatible API..."
echo "📡 API will be available at: http://$DEFAULT_HOST:$DEFAULT_PORT"
echo "🌐 Web UI will be available at: http://$DEFAULT_HOST:$DEFAULT_PORT"
echo "🔗 OpenAI-compatible endpoints:"
echo "   - GET  /v1/models"
echo "   - POST /v1/completions"
echo "   - POST /v1/chat/completions"

# Start the server in background for health check
python -m inference_node.server $ARGS &
SERVER_PID=$!

# Wait for service to be ready
if health_check $DEFAULT_PORT; then
    echo "🎉 LlamaNet OpenAI-Compatible Inference Node is running!"
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
