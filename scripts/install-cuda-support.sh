#!/bin/bash
# CUDA Support Installation Script for Python 3.12
# This script installs llama-cpp-python with CUDA support

set -e

echo "Installing CUDA support for LlamaNet on Python 3.12..."

# Check Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Detected Python version: $PYTHON_VERSION"

if [[ "$PYTHON_VERSION" < "3.8" ]]; then
    echo "Error: Python 3.8 or higher required"
    exit 1
fi

# Check for CUDA installation
if command -v nvcc &> /dev/null; then
    CUDA_VERSION=$(nvcc --version | grep "release" | sed 's/.*release \([0-9]\+\.[0-9]\+\).*/\1/')
    echo "CUDA version detected: $CUDA_VERSION"
else
    echo "Warning: CUDA not detected. Installing CPU-only version."
    pip install llama-cpp-python>=0.2.11
    exit 0
fi

# Install with CUDA support
echo "Installing llama-cpp-python with CUDA support..."
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python>=0.2.11 --force-reinstall --no-cache-dir

# Install NVIDIA monitoring
echo "Installing NVIDIA GPU monitoring..."
pip install nvidia-ml-py3>=7.352.0

# Verify installation
echo "Verifying CUDA installation..."
python3 -c "
try:
    import llama_cpp
    import pynvml
    pynvml.nvmlInit()
    device_count = pynvml.nvmlDeviceGetCount()
    print(f'✓ CUDA support verified: {device_count} GPU(s) detected')
except Exception as e:
    print(f'✗ CUDA verification failed: {e}')
    exit(1)
"

echo "✓ CUDA support installation completed successfully!"