# LlamaNet Docker Setup

This Docker configuration provides unified support for both GPU and CPU environments with automatic hardware detection for the LlamaNet distributed inference network.

## Features

- **Automatic GPU Detection**: Detects NVIDIA GPUs and installs appropriate CUDA support
- **CPU Fallback**: Automatically falls back to optimized CPU mode if no GPU is available
- **Environment Configuration**: Configurable via environment variables
- **Health Monitoring**: Built-in health checks and monitoring
- **Multi-node Support**: Easy scaling with docker-compose
- **OpenAI-Compatible API**: Full compatibility with OpenAI API endpoints
- **Real-time Network Discovery**: DHT-based peer discovery and load balancing

## Quick Start

### Prerequisites

1. **Docker and Docker Compose**: Install latest versions
   ```bash
   # Ubuntu/Debian
   sudo apt update && sudo apt install docker.io docker-compose-plugin
   
   # Or use Docker Desktop on macOS/Windows
   ```

2. **For GPU support**: NVIDIA Container Toolkit (optional)
   ```bash
   # Install NVIDIA Container Toolkit (Ubuntu/Debian)
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo systemctl restart docker
   ```

3. **Model file**: Download a GGUF model file to `./models/` directory
   ```bash
   mkdir -p models
   # Download your preferred model, e.g.:
   # wget -O models/model.gguf https://huggingface.co/microsoft/DialoGPT-medium/resolve/main/pytorch_model.bin
   # Or use any GGUF format model from Hugging Face
   ```

### Using Docker Compose (Recommended)

The current docker-compose.yml provides a basic 3-node setup:

```bash
# Start the basic network (bootstrap + 2 additional nodes)
docker-compose -f docker/docker-compose.yml up -d

# View logs from all services
docker-compose -f docker/docker-compose.yml logs -f

# View logs from specific service
docker-compose -f docker/docker-compose.yml logs -f bootstrap

# Stop all services
docker-compose -f docker/docker-compose.yml down

# Stop and remove volumes
docker-compose -f docker/docker-compose.yml down -v
```

### Using Docker Build and Run

#### Build the Image
```bash
# Build the unified image
docker build -t llamanet .
```

#### GPU Mode (Auto-detect)
```bash
# Run with GPU support (auto-detection)
docker run -d \
  --name llamanet-gpu \
  --gpus all \
  -p 8000:8000 \
  -p 8001:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e HARDWARE_MODE=auto \
  llamanet
```

#### CPU Mode (Forced)
```bash
# Run in CPU-only mode
docker run -d \
  --name llamanet-cpu \
  -p 8002:8000 \
  -p 8003:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e HARDWARE_MODE=cpu \
  -e PORT=8000 \
  -e DHT_PORT=8001 \
  llamanet
```

#### Bootstrap Node Setup
```bash
# Start bootstrap node (first node in network)
docker run -d \
  --name llamanet-bootstrap \
  --gpus all \
  -p 8000:8000 \
  -p 8001:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e NODE_ID=bootstrap-node \
  -e BOOTSTRAP_NODES="" \
  llamanet
```

#### Additional Nodes
```bash
# Start additional node that connects to bootstrap
docker run -d \
  --name llamanet-node-2 \
  --gpus all \
  -p 8002:8000 \
  -p 8003:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e NODE_ID=node-2 \
  -e BOOTSTRAP_NODES=localhost:8001 \
  llamanet
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PATH` | *required* | Path to GGUF model file |
| `HOST` | `0.0.0.0` | Host to bind the service |
| `PORT` | `8000` | HTTP API port |
| `DHT_PORT` | `8001` | DHT protocol port |
| `HARDWARE_MODE` | `auto` | Hardware mode: `auto`, `gpu`, or `cpu` |
| `N_GPU_LAYERS` | `auto` | Number of layers to offload to GPU |
| `BOOTSTRAP_NODES` | `""` | Comma-separated bootstrap nodes (ip:port) |
| `NODE_ID` | `auto` | Unique node identifier |
| `N_CTX` | `2048` | Context size for the model |
| `N_BATCH` | `8` | Batch size for processing |

## Hardware Detection Process

The container automatically detects and configures hardware:

1. **GPU Detection**: 
   - Checks for `nvidia-smi` availability
   - Verifies GPU accessibility and memory
   - Tests CUDA runtime functionality

2. **Installation**:
   - **GPU Mode**: Installs llama-cpp-python with CUDA support (`CMAKE_ARGS="-DLLAMA_CUBLAS=ON"`)
   - **CPU Mode**: Installs llama-cpp-python with OpenBLAS optimization (`CMAKE_ARGS="-DLLAMA_BLAS=ON"`)

3. **Verification**:
   - Tests the installation by importing llama_cpp
   - Sets appropriate environment variables (`N_GPU_LAYERS`, `HARDWARE_MODE`)

4. **Fallback**:
   - Automatically falls back to CPU if GPU setup fails
   - Provides clear error messages and logs

## API Endpoints

Once running, the following OpenAI-compatible endpoints are available:

### Core OpenAI API
- `GET /v1/models` - List available models
- `POST /v1/completions` - Text completions
- `POST /v1/chat/completions` - Chat completions (with streaming support)

### LlamaNet Extensions
- `GET /v1/models/network` - List all models across the network
- `GET /models/statistics` - Network model statistics and performance metrics

### Status and Monitoring
- `GET /` - Web UI (if available)
- `GET /health` - Health check endpoint
- `GET /status` - Node status and metrics
- `GET /info` - Node information and system details
- `GET /nodes` - Discovered network nodes
- `GET /dht/status` - DHT network status

### Real-time Updates
- `GET /events/network` - Server-Sent Events for real-time network updates

## Web Interface

LlamaNet includes a built-in web interface accessible at `http://localhost:8000/` that provides:

- **Real-time Chat Interface**: OpenAI-compatible chat with streaming support
- **Network Monitoring**: Live view of discovered nodes and models
- **Load Balancing Controls**: Choose routing strategies (round-robin, load-balanced, random)
- **Model Selection**: Select specific models across the network
- **Performance Metrics**: View node performance and network health

## Network Architecture

LlamaNet uses a distributed hash table (DHT) based on Kademlia for peer discovery:

1. **Bootstrap Node**: First node that others connect to
2. **Peer Discovery**: Automatic discovery of other nodes via DHT
3. **Load Balancing**: Intelligent request routing based on node load
4. **Fault Tolerance**: Automatic failover if nodes become unavailable

## Monitoring and Debugging

### Health Checks
```bash
# Check container health
docker ps

# Check application health
curl http://localhost:8000/health

# Check node status and metrics
curl http://localhost:8000/status

# Check network discovery
curl http://localhost:8000/nodes
```

### Logs and Debugging
```bash
# View container logs
docker logs llamanet-bootstrap

# Follow logs in real-time
docker logs -f llamanet-bootstrap

# Check hardware detection
docker logs llamanet-bootstrap | grep -E "(GPU|CPU|Hardware)"

# Debug network connectivity
curl http://localhost:8000/dht/status
curl http://localhost:8000/debug/routing
```

### Resource Monitoring
```bash
# Monitor GPU usage (if available)
nvidia-smi

# Monitor container resources
docker stats

# Check disk usage
docker system df
```

## Testing the API

### Using curl
```bash
# List models
curl http://localhost:8000/v1/models

# Chat completion
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llamanet",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100,
    "temperature": 0.7
  }'

# Streaming chat completion
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llamanet",
    "messages": [{"role": "user", "content": "Tell me a story"}],
    "max_tokens": 200,
    "stream": true
  }'

# Network-wide model listing
curl http://localhost:8000/v1/models/network
```

### Load Balancing Strategies
```bash
# Round-robin routing
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llamanet",
    "messages": [{"role": "user", "content": "Hello!"}],
    "strategy": "round_robin"
  }'

# Load-balanced routing
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llamanet",
    "messages": [{"role": "user", "content": "Hello!"}],
    "strategy": "load_balanced"
  }'
```

## Scaling and Load Balancing

### Add More Nodes
```bash
# Add another GPU node
docker run -d \
  --name llamanet-gpu-3 \
  --gpus all \
  -p 8006:8000 \
  -p 8007:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e NODE_ID=gpu-node-3 \
  -e BOOTSTRAP_NODES=localhost:8001 \
  llamanet

# Add a CPU-only node
docker run -d \
  --name llamanet-cpu-4 \
  -p 8008:8000 \
  -p 8009:8001/udp \
  -v $(pwd)/models:/models:ro \
  -e MODEL_PATH=/models/your-model.gguf \
  -e HARDWARE_MODE=cpu \
  -e NODE_ID=cpu-node-4 \
  -e BOOTSTRAP_NODES=localhost:8001,localhost:8003 \
  llamanet
```

### Load Testing
```bash
# Test the network with multiple concurrent requests
for i in {1..10}; do
  curl -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
      "model": "llamanet",
      "messages": [{"role": "user", "content": "Hello, world!"}],
      "max_tokens": 50,
      "strategy": "round_robin"
    }' &
done
wait
```

## Troubleshooting

### Common Issues

#### GPU Not Detected
```bash
# Check NVIDIA driver
nvidia-smi

# Check Docker GPU support
docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi

# Force CPU mode if needed
docker run ... -e HARDWARE_MODE=cpu ...

# Check container GPU access
docker exec llamanet-bootstrap nvidia-smi
```

#### Model File Issues
```bash
# Check model file exists and is accessible
ls -la models/
docker run --rm -v $(pwd)/models:/models:ro ubuntu ls -la /models/

# Check model file format
file models/your-model.gguf

# Verify model path in container
docker exec llamanet-bootstrap ls -la /models/
```

#### Network Connectivity Issues
```bash
# Check DHT status
curl http://localhost:8000/dht/status

# Check node discovery
curl http://localhost:8000/nodes

# Check container networking
docker network ls
docker network inspect docker_default

# Debug routing
curl http://localhost:8000/debug/routing

# Verify DHT network
curl http://localhost:8000/dht/verify
```

#### Performance Issues
```bash
# Monitor resource usage
docker stats

# Check GPU utilization
nvidia-smi -l 1

# Adjust GPU layers based on available memory
docker run ... -e N_GPU_LAYERS=16 ...

# Check node performance
curl http://localhost:8000/status
curl http://localhost:8000/models/statistics
```

#### Container Startup Issues
```bash
# Check container logs
docker logs llamanet-bootstrap

# Check hardware detection logs
docker logs llamanet-bootstrap | grep -E "(Hardware|GPU|CPU|Model)"

# Verify environment variables
docker exec llamanet-bootstrap env | grep -E "(MODEL_PATH|HARDWARE_MODE|N_GPU_LAYERS)"

# Test model loading
docker exec llamanet-bootstrap python -c "from llama_cpp import Llama; print('Model loading test')"
```

### Getting Help

1. **Check logs**: Always start with container logs using `docker logs`
2. **Verify setup**: Ensure all prerequisites are met (Docker, model files, GPU drivers)
3. **Test components**: Test GPU, model file, and network separately
4. **Use CPU fallback**: If GPU issues persist, force CPU mode with `HARDWARE_MODE=cpu`
5. **Check documentation**: Review the main project README and API documentation

## Performance Optimization

### GPU Optimization
- **Adjust GPU layers**: Set `N_GPU_LAYERS` based on your GPU memory (start with 32, reduce if OOM)
- **Monitor memory**: Use `nvidia-smi` to monitor GPU memory usage
- **Model size**: Use appropriate model sizes for your hardware
- **Batch size**: Adjust `N_BATCH` for optimal throughput

### CPU Optimization
- **OpenBLAS**: Ensure OpenBLAS is properly configured (automatic in CPU mode)
- **Thread count**: CPU inference will use available cores automatically
- **Context size**: Reduce `N_CTX` if memory is limited
- **Model quantization**: Use smaller quantized models for better CPU performance

### Network Optimization
- **Fast storage**: Use SSD storage for model files
- **Network latency**: Place nodes close to each other for low-latency communication
- **Load balancing**: Use appropriate strategies based on your workload
- **Connection pooling**: The system automatically manages connections

### Docker Optimization
```bash
# Increase shared memory for better performance
docker run --shm-size=1g ...

# Set CPU affinity for dedicated nodes
docker run --cpuset-cpus="0-3" ...

# Limit memory usage
docker run --memory=8g ...

# Use host networking for minimal latency
docker run --network=host ...
```

## Production Deployment

### Security Considerations
- Use proper firewall rules to restrict DHT port access
- Consider using Docker secrets for sensitive configuration
- Implement proper logging and monitoring
- Use non-root users in containers when possible

### High Availability
- Deploy multiple bootstrap nodes for redundancy
- Use container orchestration (Kubernetes, Docker Swarm) for automatic restarts
- Implement health checks and automatic failover
- Monitor network partitions and node failures

### Monitoring and Alerting
- Set up monitoring for container health, resource usage, and API response times
- Use the built-in metrics endpoints for integration with monitoring systems
- Implement alerting for node failures and performance degradation
- Monitor DHT network health and connectivity

## Integration Examples

### Python Client
```python
import openai

# Configure client to use LlamaNet
openai.api_base = "http://localhost:8000/v1"
openai.api_key = "not-needed"

# Use standard OpenAI API
response = openai.ChatCompletion.create(
    model="llamanet",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=100
)

print(response.choices[0].message.content)
```

### Node.js Client
```javascript
const { Configuration, OpenAIApi } = require("openai");

const configuration = new Configuration({
  basePath: "http://localhost:8000/v1",
  apiKey: "not-needed",
});

const openai = new OpenAIApi(configuration);

async function chat() {
  const response = await openai.createChatCompletion({
    model: "llamanet",
    messages: [{ role: "user", content: "Hello!" }],
    max_tokens: 100,
  });
  
  console.log(response.data.choices[0].message.content);
}

chat();
```

This Docker setup provides a complete, production-ready distributed inference network that's fully compatible with OpenAI APIs while offering the benefits of distributed computing and automatic load balancing.
