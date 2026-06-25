# CBN Analytics — React chat frontend

A chat-focused React + Vite + TypeScript UI for the Superset MCP agent. Replaces
the single-file `agent/index.html`. The Flask backend (`agent/pipeline.py`) keeps
serving the API (`/auth-config`, `/run` SSE, `/guest-token`); this app is the
view layer.

## Architecture

```
src/
  config.ts            BASE-path helper (handles the /chatbot sub-path)
  types.ts             SSE event + chat message types, phase list, examples
  api.ts               auth-config, runPipeline (SSE), fetchGuestToken
  auth/keycloak.ts     keycloak-js init (PKCE, login-required); no-ops when disabled
  context.tsx          embed config + username via React context
  hooks/usePipeline.ts owns the transcript; streams /run into chat messages
  components/
    App.tsx            boot (auth-config → Keycloak), layout
    Header.tsx         brand + user/sign-out
    ChatView.tsx       welcome state + message list + composer (auto-scroll)
    Composer.tsx       textarea + send + example chips
    Message.tsx        user bubble; assistant run (phase strip, logs, embed)
    DashboardEmbed.tsx inline dashboard via @superset-ui/embedded-sdk + guest token
```

## Run locally

**1. Backend** (needs Python 3.10+; the repo's default `python3` may be 3.9):

```bash
# from repo root
uv venv --python python3.13 .venv-local
uv pip install --python .venv-local/bin/python -r agent/requirements.txt

# .env (repo root) must have real values:
#   OPENAI_API_KEY=...         (required for plan generation)
#   MCP_AUTH_TOKEN=...          (hosted MCP)
#   SUPERSET_USERNAME=admin@superset.com
#   SUPERSET_PASSWORD=...       (for guest-token embed)
KEYCLOAK_ENABLED=false APP_BASE_PATH= .venv-local/bin/python agent/pipeline.py --port 5001
```

`KEYCLOAK_ENABLED=false` skips SSO so you can use the app without a Keycloak login.

**2. Frontend:**

```bash
cd frontend
pnpm install
pnpm dev            # http://localhost:5173 — proxies API routes to :5001
```

Override the backend with `VITE_BACKEND=http://host:port pnpm dev`.

## Production build (sub-path deploy)

```bash
VITE_BASE=/chatbot/ pnpm build     # → frontend/dist
```

`base=/chatbot/` makes all asset + API URLs resolve under the proxy prefix. The
Flask app can then serve `frontend/dist` (wiring TBD — currently it serves the
legacy `agent/index.html`).
