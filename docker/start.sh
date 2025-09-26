#!/bin/bash
# Startup script for LlamaNet Docker container
# Orchestrates hardware detection, configuration, and application startup

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Logging functions
log() {
    echo -e "${BLUE}[STARTUP]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[STARTUP]${NC} ✅ $1"
}

log_warning() {
    echo -e "${YELLOW}[STARTUP]${NC} ⚠️  $1"
}

log_error() {
    echo -e "${RED}[STARTUP]${NC} ❌ $1"
}

log_info() {
    echo -e "${CYAN}[STARTUP]${NC} ℹ️  $1"
}

show_banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                    🦙 LlamaNet Container                      ║${NC}"
    echo -e "${CYAN}║              Distributed AI Inference Network               ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

validate_environment() {
    log "🔍 Validating environment configuration..."
    
    local validation_failed=false
    
    # Check required environment variables
    if [ -z "$MODEL_PATH" ]; then
        log_error "MODEL_PATH environment variable is required"
        log_info "Please set MODEL_PATH to point to your GGUF model file"
        log_info "Example: -e MODEL_PATH=/models/your-model.gguf"
        validation_failed=true
    fi
    
    # Check if model file exists
    if [ -n "$MODEL_PATH" ] && [ ! -f "$MODEL_PATH" ]; then
        log_error "Model file not found at $MODEL_PATH"
        log_info "Available files in /models:"
        if [ -d "/models" ]; then
            ls -la /models/ 2>/dev/null || log_warning "Models directory is empty or not accessible"
        else
            log_warning "Models directory not mounted"
        fi
        validation_failed=true
    fi
    
    # Validate model file format
    if [ -n "$MODEL_PATH" ] && [ -f "$MODEL_PATH" ]; then
        if [[ "$MODEL_PATH" == *.gguf ]]; then
            log_success "Model file found: $MODEL_PATH"
            log_info "📁 Model size: $(du -h "$MODEL_PATH" | cut -f1)"
        else
            log_warning "Model file does not have .gguf extension: $MODEL_PATH"
            log_info "LlamaNet expects GGUF format models"
        fi
    fi
    
    # Validate ports
    local port=${PORT:-8000}
    local dht_port=${DHT_PORT:-8001}
    
    if [ "$port" = "$dht_port" ]; then
        log_error "HTTP port ($port) and DHT port ($dht_port) cannot be the same"
        validation_failed=true
    fi
    
    # Check for port conflicts (basic check)
    if netstat -tuln 2>/dev/null | grep -q ":$port "; then
        log_warning "Port $port appears to be in use"
    fi
    
    if netstat -tuln 2>/dev/null | grep -q ":$dht_port "; then
        log_warning "DHT port $dht_port appears to be in use"
    fi
    
    if [ "$validation_failed" = true ]; then
        log_error "Environment validation failed"
        exit 1
    fi
    
    log_success "Environment validation passed"
}

run_hardware_detection() {
    log "🔧 Running hardware detection and setup..."
    
    # Source and run GPU detection script
    if [ -f "/usr/local/bin/gpu-detect.sh" ]; then
        source /usr/local/bin/gpu-detect.sh
        
        # Run the main detection function
        if ! main; then
            log_error "Hardware detection failed"
            exit 1
        fi
        
        # Load the hardware configuration
        if [ -f "/tmp/hardware_config" ]; then
            source /tmp/hardware_config
            log_success "Hardware configuration loaded"
        else
            log_warning "Hardware configuration file not found, using defaults"
            export HARDWARE_MODE=${HARDWARE_MODE:-cpu}
            export N_GPU_LAYERS=${N_GPU_LAYERS:-0}
        fi
    else
        log_error "GPU detection script not found at /usr/local/bin/gpu-detect.sh"
        exit 1
    fi
}

setup_python_environment() {
    log "🐍 Setting up Python environment..."
    
    # Set Python path and ensure UTF-8 encoding
    export PYTHONPATH=/app:$PYTHONPATH
    export PYTHONUNBUFFERED=1
    export PYTHONIOENCODING=utf-8
    export LC_ALL=C.UTF-8
    export LANG=C.UTF-8
    
    # Verify Python installation
    if ! python3 --version >/dev/null 2>&1; then
        log_error "Python 3 is not available"
        exit 1
    fi
    
    log_success "Python environment configured"
    log_info "Python version: $(python3 --version)"
}

configure_application() {
    log "⚙️  Configuring LlamaNet application..."
    
    # Set default values for environment variables
    export HOST=${HOST:-0.0.0.0}
    export PORT=${PORT:-8000}
    export DHT_PORT=${DHT_PORT:-8001}
    export NODE_ID=${NODE_ID:-$(hostname)-$(date +%s)}
    export BOOTSTRAP_NODES=${BOOTSTRAP_NODES:-""}
    
    # Hardware-specific configuration
    export N_GPU_LAYERS=${N_GPU_LAYERS:-0}
    export HARDWARE_MODE=${HARDWARE_MODE:-cpu}
    
    # Performance tuning based on hardware
    if [ "$HARDWARE_MODE" = "gpu" ]; then
        # GPU-specific optimizations
        export N_BATCH=${N_BATCH:-512}
        export N_CTX=${N_CTX:-4096}
        export N_THREADS=${N_THREADS:-$(nproc)}
    else
        # CPU-specific optimizations
        export N_BATCH=${N_BATCH:-128}
        export N_CTX=${N_CTX:-2048}
        export N_THREADS=${N_THREADS:-$(nproc)}
    fi
    
    # Logging configuration
    export LOG_LEVEL=${LOG_LEVEL:-info}
    
    log_success "Application configuration complete"
}

show_configuration() {
    echo ""
    log "🚀 LlamaNet Configuration Summary"
    log "=================================="
    echo -e "  ${CYAN}Hardware Mode:${NC} $HARDWARE_MODE"
    echo -e "  ${CYAN}GPU Layers:${NC} $N_GPU_LAYERS"
    echo -e "  ${CYAN}Model Path:${NC} $MODEL_PATH"
    echo -e "  ${CYAN}Host:${NC} $HOST"
    echo -e "  ${CYAN}HTTP Port:${NC} $PORT"
    echo -e "  ${CYAN}DHT Port:${NC} $DHT_PORT"
    echo -e "  ${CYAN}Node ID:${NC} $NODE_ID"
    echo -e "  ${CYAN}Bootstrap Nodes:${NC} ${BOOTSTRAP_NODES:-'none (bootstrap node)'}"
    echo -e "  ${CYAN}Context Size:${NC} $N_CTX"
    echo -e "  ${CYAN}Batch Size:${NC} $N_BATCH"
    echo -e "  ${CYAN}Threads:${NC} $N_THREADS"
    log "=================================="
    echo ""
}

setup_signal_handlers() {
    log "📡 Setting up signal handlers..."
    
    # Function to handle shutdown signals
    shutdown_handler() {
        log_warning "Received shutdown signal, cleaning up..."
        
        # Kill any background processes
        jobs -p | xargs -r kill 2>/dev/null || true
        
        log_success "Cleanup complete, exiting"
        exit 0
    }
    
    # Set up signal traps
    trap shutdown_handler SIGTERM SIGINT SIGQUIT
    
    log_success "Signal handlers configured"
}

perform_health_checks() {
    log "🏥 Performing pre-startup health checks..."
    
    # Check disk space
    local available_space=$(df /app | tail -1 | awk '{print $4}')
    if [ "$available_space" -lt 1048576 ]; then  # Less than 1GB
        log_warning "Low disk space available: $(df -h /app | tail -1 | awk '{print $4}')"
    fi
    
    # Check memory
    local available_memory=$(free -m | grep '^Mem:' | awk '{print $7}')
    if [ "$available_memory" -lt 1024 ]; then  # Less than 1GB
        log_warning "Low memory available: ${available_memory}MB"
    fi
    
    # Test model file accessibility
    if [ -f "$MODEL_PATH" ]; then
        if [ -r "$MODEL_PATH" ]; then
            log_success "Model file is readable"
        else
            log_error "Model file is not readable"
            exit 1
        fi
    fi
    
    # Test network connectivity (if bootstrap nodes specified)
    if [ -n "$BOOTSTRAP_NODES" ]; then
        log_info "Testing connectivity to bootstrap nodes..."
        IFS=',' read -ra NODES <<< "$BOOTSTRAP_NODES"
        for node in "${NODES[@]}"; do
            IFS=':' read -ra ADDR <<< "$node"
            local host=${ADDR[0]}
            local port=${ADDR[1]}
            
            if timeout 5 nc -z "$host" "$port" 2>/dev/null; then
                log_success "Bootstrap node reachable: $node"
            else
                log_warning "Bootstrap node not reachable: $node"
            fi
        done
    fi
    
    log_success "Health checks completed"
}

start_application() {
    log "🎬 Starting LlamaNet application..."
    
    # Change to application directory
    cd /app
    
    # Show final startup message
    log_success "Launching: $*"
    echo ""
    
    # Execute the application with all provided arguments
    exec "$@"
}

# Main startup sequence
main() {
    show_banner
    
    # Core startup sequence
    validate_environment
    setup_python_environment
    setup_signal_handlers
    run_hardware_detection
    configure_application
    perform_health_checks
    show_configuration
    
    # Start the application
    start_application "$@"
}

# Run main function with all arguments
main "$@"
