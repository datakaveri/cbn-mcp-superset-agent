# Deployment — Superset MCP Agent (web UI)

This deploys the Flask web UI ([agent/pipeline.py](../agent/pipeline.py)) as a
container. It serves the browser UI, the `POST /run` SSE pipeline endpoint, the
Keycloak `/auth-config`, and the PWA assets on port **5001**.

> The TUI (`main.py`) and headless (`main.py --query …`) modes are not part of
> this container — it ships the web UI only.

## What you need

- Docker (with Compose v2)
- An **OpenAI API key** (`OPENAI_API_KEY`)
- A **Bearer token** for the hosted Superset MCP (`MCP_AUTH_TOKEN`)
- **Superset** credentials (`SUPERSET_*`) — the pipeline's health check logs into Superset
- A **Keycloak** client. Defaults reuse the shared `cbn` / `angular-client`.

## Quick start (Docker Compose)

```bash
cd infra
cp .env.example .env          # then edit .env and fill in the secrets
docker compose up -d --build
```

UI → http://localhost:5001 · health → http://localhost:5001/health

## Build/run with Docker directly

The build context is the **repo root** (the Dockerfile copies `agent/`):

```bash
# from the repo root
docker build -f infra/Dockerfile -t cbn-mcp-superset-agent .
docker run -d --name cbn-mcp-agent-ui -p 5001:5001 --env-file infra/.env \
  cbn-mcp-superset-agent
```

## Configuration

All variables and defaults are documented in [.env.example](.env.example) and in
the root [README](../README.md#configuration). Only secrets and the Superset
password lack defaults; everything else has a sensible default.

| Must set | Why |
|---|---|
| `OPENAI_API_KEY` | LLM calls fail without it |
| `MCP_AUTH_TOKEN` | hosted MCP rejects requests without it |
| `SUPERSET_PASSWORD` | health check logs into Superset |

## ⚠️ Keycloak one-time setup (required)

The UI reuses the `angular-client` Keycloak client. Before login works, an admin
must add this deployment's **public origin** to that client in Keycloak:

- **Valid Redirect URIs:** `https://<your-host>/*` (and `http://localhost:5001/*` for local)
- **Web Origins:** `https://<your-host>` (and `http://localhost:5001`)

Otherwise the browser login redirect is rejected. To run without auth (local
testing only), set `KEYCLOAK_ENABLED=false`.

## Behind a reverse proxy / TLS

Terminate TLS at your ingress/nginx and forward to the container's `:5001`.
The `POST /run` endpoint streams Server-Sent Events, so disable response
buffering on that path (the app already sends `X-Accel-Buffering: no` for nginx).
Use the HTTPS origin in the Keycloak redirect URIs above.

## Notes

- Base image is `python:3.11-slim` — the app requires **Python 3.11+** (it uses
  `X | None` typing).
- Logs go to **stdout** (`docker logs`) as well as `agent.log` inside the container.
- The Flask server runs threaded and handles the SSE streaming for this internal
  tool. For higher concurrency, front it with multiple replicas behind the proxy,
  or switch to a gunicorn entrypoint (`gthread` workers, `--timeout 0`).
- Never bake `.env` into the image — it is excluded via [.dockerignore](../.dockerignore).
