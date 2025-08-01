FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Environment variables
ENV REGISTRY_PORT=8080

# Expose port
EXPOSE 8080

# Run the registry service
CMD ["python", "-m", "registry.server"]
