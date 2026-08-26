# Dockerfile.ml
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

# Install system dependencies for Polars/DuckDB/XGBoost if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Create spill directory for DuckDB
RUN mkdir -p /tmp/duckdb_spill && chmod 777 /tmp/duckdb_spill

ENV PYTHONPATH=/app
CMD ["python", "src/pipeline.py"]
