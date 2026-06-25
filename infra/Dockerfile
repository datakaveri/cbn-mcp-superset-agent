# Superset MCP Agent — web UI container
# Build from the REPO ROOT (build context = repo root):
#   docker build -f infra/Dockerfile -t cbn-mcp-superset-agent .
# Override the sub-path with --build-arg VITE_BASE=/ for a root deploy.

# ── Stage 1: build the React frontend ────────────────────────────────
# Build on the native BUILD platform (not the --platform target): the output is
# static JS/CSS, so this avoids slow QEMU emulation when cross-building for amd64.
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend
WORKDIR /build
RUN npm install -g pnpm@10
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
# Sub-path build so asset + API URLs resolve under the proxy prefix (must match
# APP_BASE_PATH on the Python service). Defaults to /chatbot/.
ARG VITE_BASE=/chatbot/
ENV VITE_BASE=${VITE_BASE}
RUN pnpm build

# ── Stage 2: Python runtime ──────────────────────────────────────────
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
# Built React app — Flask serves it from /app/frontend/dist
COPY --from=frontend /build/dist ./frontend/dist

WORKDIR /app/agent

EXPOSE 5001

# Liveness probe against the public /health endpoint (no curl in slim image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=3).status==200 else 1)"

# Serves the React app + /run (SSE) + auth + guest-token + PWA on 0.0.0.0:5001
CMD ["python", "pipeline.py", "--port", "5001", "--host", "0.0.0.0"]
