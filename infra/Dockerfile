# Superset MCP Agent — web UI container
# Build from the REPO ROOT (build context = repo root):
#   docker build -f infra/Dockerfile -t cbn-mcp-superset-agent .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY agent/ ./agent/
WORKDIR /app/agent

EXPOSE 5001

# Liveness probe against the public /health endpoint (no curl in slim image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=3).status==200 else 1)"

# Serves index.html + /run (SSE) + auth + PWA assets on 0.0.0.0:5001
CMD ["python", "pipeline.py", "--port", "5001", "--host", "0.0.0.0"]
