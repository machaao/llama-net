#!/bin/bash

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

# Set default values
DEFAULT_MODEL_PATH="${MODEL_PATH:-./models/model.gguf}"
DEFAULT_HOST="${HOST:-0.0.0.0}"
DEFAULT_PORT="${PORT:-8000}"
DEFAULT_DHT_PORT="${DHT_PORT:-8001}"
DEFAULT_NODE_ID="${NODE_ID:-}"
DEFAULT_BOOTSTRAP_NODES="${BOOTSTRAP_NODES:-}"

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
    
    # Keep the server running in foreground
    wait $SERVER_PID
else
    echo "❌ Failed to start service"
    kill $SERVER_PID 2>/dev/null || true
    exit 1
fi
