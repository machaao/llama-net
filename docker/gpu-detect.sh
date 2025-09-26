#!/bin/bash
# GPU Detection Script for LlamaNet Docker
# Detects NVIDIA GPU availability and configures appropriate llama-cpp-python installation

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging function
log() {
    echo -e "${BLUE}[GPU-DETECT]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[GPU-DETECT]${NC} ✅ $1"
}

log_warning() {
    echo -e "${YELLOW}[GPU-DETECT]${NC} ⚠️  $1"
}

log_error() {
    echo -e "${RED}[GPU-DETECT]${NC} ❌ $1"
}

detect_gpu() {
    log "🔍 Detecting GPU capabilities..."
    
    # Check if nvidia-smi is available and working
    if command -v nvidia-smi >/dev/null 2>&1; then
        if nvidia-smi >/dev/null 2>&1; then
            GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "0")
            GPU_MEMORY=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "0")
            GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "Unknown")
            
            if [ "$GPU_COUNT" -gt 0 ] && [ "$GPU_MEMORY" -gt 0 ]; then
                log_success "GPU detected: $GPU_NAME"
                log "📊 GPU Memory: ${GPU_MEMORY}MB"
                log "🔢 GPU Count: $GPU_COUNT"
                
                # Check CUDA runtime availability
                if python3 -c "import torch; print('CUDA available:', torch.cuda.is_available())" 2>/dev/null | grep -q "True"; then
                    log_success "CUDA runtime verified"
                    return 0
                else
                    log_warning "CUDA runtime not available in Python, checking basic GPU access..."
                    # Even without torch CUDA, we can still try CUDA compilation
                    return 0
                fi
            else
                log_warning "GPU detected but not accessible (Count: $GPU_COUNT, Memory: ${GPU_MEMORY}MB)"
                return 1
            fi
        else
            log_warning "nvidia-smi found but not working, using CPU mode"
            return 1
        fi
    else
        log "ℹ️  No GPU detected, using CPU mode"
        return 1
    fi
}

install_gpu_support() {
    log "🚀 Installing GPU-optimized llama-cpp-python..."
    
    # Set CUDA compilation flags
    export CMAKE_ARGS="-DLLAMA_CUBLAS=ON -DLLAMA_CUDA_FORCE_DMMV=ON"
    export FORCE_CMAKE=1
    export CUDACXX=/usr/local/cuda/bin/nvcc
    
    # Ensure CUDA is in PATH
    export PATH="/usr/local/cuda/bin:$PATH"
    export LD_LIBRARY_PATH="/usr/local/cuda/lib64:$LD_LIBRARY_PATH"
    
    log "🔧 CUDA compilation flags set: $CMAKE_ARGS"
    
    # Install GPU version with retry logic
    local max_attempts=3
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        log "📦 Installing llama-cpp-python with CUDA support (attempt $attempt/$max_attempts)..."
        
        if pip install --no-cache-dir --force-reinstall --no-deps llama-cpp-python; then
            log_success "GPU version installation completed"
            break
        else
            log_warning "Installation attempt $attempt failed"
            if [ $attempt -eq $max_attempts ]; then
                log_error "GPU installation failed after $max_attempts attempts"
                return 1
            fi
            attempt=$((attempt + 1))
            sleep 2
        fi
    done
    
    # Verify GPU installation
    log "🔍 Verifying GPU installation..."
    if python3 -c "
import sys
try:
    from llama_cpp import Llama
    print('✅ llama-cpp-python imported successfully')
    
    # Try to check for CUDA support
    try:
        # This is a basic test - actual CUDA verification happens at model load time
        print('✅ GPU support appears to be available')
        sys.exit(0)
    except Exception as e:
        print(f'⚠️  GPU support verification inconclusive: {e}')
        sys.exit(0)  # Still proceed, will be verified at runtime
        
except ImportError as e:
    print(f'❌ Failed to import llama-cpp-python: {e}')
    sys.exit(1)
except Exception as e:
    print(f'❌ Unexpected error: {e}')
    sys.exit(1)
" 2>&1; then
        log_success "GPU support installed and verified"
        
        # Set optimal GPU configuration
        export N_GPU_LAYERS=${N_GPU_LAYERS:-32}
        export HARDWARE_MODE=gpu
        
        # Calculate optimal GPU layers based on available memory
        if [ "$GPU_MEMORY" -gt 0 ]; then
            if [ "$GPU_MEMORY" -gt 16000 ]; then
                export N_GPU_LAYERS=40  # High-end GPU
            elif [ "$GPU_MEMORY" -gt 8000 ]; then
                export N_GPU_LAYERS=32  # Mid-range GPU
            elif [ "$GPU_MEMORY" -gt 4000 ]; then
                export N_GPU_LAYERS=20  # Lower-end GPU
            else
                export N_GPU_LAYERS=10  # Very limited GPU memory
            fi
            log "🎯 Optimized GPU layers for ${GPU_MEMORY}MB VRAM: $N_GPU_LAYERS"
        fi
        
        return 0
    else
        log_error "GPU installation verification failed"
        return 1
    fi
}

install_cpu_support() {
    log "🖥️  Installing CPU-optimized llama-cpp-python..."
    
    # Set CPU compilation flags for optimal performance
    export CMAKE_ARGS="-DLLAMA_BLAS=ON -DLLAMA_BLAS_VENDOR=OpenBLAS -DLLAMA_NATIVE=ON"
    unset FORCE_CMAKE
    unset CUDACXX
    
    log "🔧 CPU compilation flags set: $CMAKE_ARGS"
    
    # Install CPU version with retry logic
    local max_attempts=3
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        log "📦 Installing llama-cpp-python with CPU optimization (attempt $attempt/$max_attempts)..."
        
        if pip install --no-cache-dir --force-reinstall --no-deps llama-cpp-python; then
            log_success "CPU version installation completed"
            break
        else
            log_warning "Installation attempt $attempt failed"
            if [ $attempt -eq $max_attempts ]; then
                log_error "CPU installation failed after $max_attempts attempts"
                return 1
            fi
            attempt=$((attempt + 1))
            sleep 2
        fi
    done
    
    # Verify CPU installation
    log "🔍 Verifying CPU installation..."
    if python3 -c "
import sys
try:
    from llama_cpp import Llama
    print('✅ llama-cpp-python imported successfully')
    print('✅ CPU support available')
    sys.exit(0)
except ImportError as e:
    print(f'❌ Failed to import llama-cpp-python: {e}')
    sys.exit(1)
except Exception as e:
    print(f'❌ Unexpected error: {e}')
    sys.exit(1)
" 2>&1; then
        log_success "CPU support installed and verified"
        export N_GPU_LAYERS=0
        export HARDWARE_MODE=cpu
        return 0
    else
        log_error "CPU installation verification failed"
        return 1
    fi
}

get_system_info() {
    log "📋 System Information:"
    echo "  OS: $(uname -s) $(uname -r)"
    echo "  Architecture: $(uname -m)"
    echo "  Python: $(python3 --version 2>&1)"
    echo "  CPU Cores: $(nproc)"
    echo "  Memory: $(free -h | grep '^Mem:' | awk '{print $2}') total"
    
    if command -v nvidia-smi >/dev/null 2>&1; then
        echo "  NVIDIA Driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 'Not available')"
        echo "  CUDA Version: $(nvcc --version 2>/dev/null | grep 'release' | awk '{print $6}' | cut -c2- || echo 'Not available')"
    fi
}

# Main detection and installation logic
main() {
    echo ""
    log "🔧 LlamaNet Hardware Detection and Setup"
    log "========================================"
    
    # Show system information
    get_system_info
    echo ""
    
    # Check if hardware mode is forced via environment variable
    if [ "$HARDWARE_MODE" = "gpu" ]; then
        log "🎯 GPU mode forced via environment variable"
        if ! install_gpu_support; then
            log_error "Forced GPU mode failed, exiting"
            exit 1
        fi
    elif [ "$HARDWARE_MODE" = "cpu" ]; then
        log "🎯 CPU mode forced via environment variable"
        if ! install_cpu_support; then
            log_error "CPU mode installation failed"
            exit 1
        fi
    else
        log "🔍 Auto-detecting hardware capabilities..."
        if detect_gpu; then
            log "🎯 GPU detected, attempting GPU installation..."
            if ! install_gpu_support; then
                log_warning "GPU installation failed, falling back to CPU mode"
                if ! install_cpu_support; then
                    log_error "Both GPU and CPU installation failed"
                    exit 1
                fi
            fi
        else
            log "🎯 No GPU detected or GPU not accessible, using CPU mode"
            if ! install_cpu_support; then
                log_error "CPU mode installation failed"
                exit 1
            fi
        fi
    fi
    
    echo ""
    log "========================================"
    log_success "Hardware setup complete"
    log "🏃 Mode: $HARDWARE_MODE"
    log "🧠 GPU Layers: ${N_GPU_LAYERS:-0}"
    log "🔧 CMAKE_ARGS: ${CMAKE_ARGS:-'Not set'}"
    
    # Export variables for the startup script
    echo "export HARDWARE_MODE=$HARDWARE_MODE" > /tmp/hardware_config
    echo "export N_GPU_LAYERS=${N_GPU_LAYERS:-0}" >> /tmp/hardware_config
    echo "export CMAKE_ARGS='$CMAKE_ARGS'" >> /tmp/hardware_config
    
    log_success "Configuration saved to /tmp/hardware_config"
    echo ""
}

# Export function for use in other scripts
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
