FROM python:3.12-slim

WORKDIR /app

# Install curl for health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
# We copy all files into /app. service.py will run as the entrypoint.
COPY . .

# Environment defaults
ENV PYTHONUNBUFFERED=1
ENV CACHE_DB=/config/proxy_cache.db
ENV OB_PROXY_DB=/config/ob_meta.db
ENV PYTHONPATH=/app

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:9699/health || exit 1

CMD ["python3", "service.py"]
