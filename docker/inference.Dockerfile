FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Install package in development mode
RUN pip install -e .

# Create directory for models
RUN mkdir -p /models

# Environment variables
ENV MODEL_PATH=/models/model.gguf
ENV PORT=8000
ENV DHT_PORT=8001

# Expose ports
EXPOSE 8000 8001

# Run the inference node
CMD ["python", "-m", "inference_node.server"]
