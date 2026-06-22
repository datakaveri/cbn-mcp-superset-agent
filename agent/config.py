"""
Centralized configuration for the Superset MCP Agentic Pipeline.
All endpoints, credentials, and tuning knobs live here.
Reads from environment variables with defaults matching the local setup.
"""

import os

# Load a local .env file if present so config can be set without exporting.
# Optional dependency — if python-dotenv isn't installed, fall back to the
# process environment only.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Superset ──────────────────────────────────────────────────────────
SUPERSET_BASE_URL = os.getenv("SUPERSET_BASE_URL", "http://localhost:9001")
SUPERSET_API_URL = f"{SUPERSET_BASE_URL}/api/v1"
SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "admin")
# The pipeline drives Superset entirely through MCP (MCP_AUTH_TOKEN); the direct
# REST login is a legacy health-check gate whose token nothing consumes. Disable
# it when Superset is SSO-only (no db-provider admin login). Default true.
SUPERSET_LOGIN_ENABLED = os.getenv("SUPERSET_LOGIN_ENABLED", "true").lower() not in ("0", "false", "no")

# ── Superset embedding (inline dashboard preview via guest token) ─────
# The agent UI embeds the created dashboard with @superset-ui/embedded-sdk,
# fetching a guest token from the same middleware ui-cbn uses.
SUPERSET_EMBED_ENABLED = os.getenv("SUPERSET_EMBED_ENABLED", "true").lower() not in ("0", "false", "no")
# Public Superset origin for the embedded SDK (defaults to SUPERSET_BASE_URL).
SUPERSET_DOMAIN = os.getenv("SUPERSET_DOMAIN", SUPERSET_BASE_URL)
# Register each created dashboard for embedding via Superset REST (so /embedded/<uuid>
# resolves for the guest-token preview). Requires a working SUPERSET_USERNAME/PASSWORD
# (db provider). Non-fatal if it fails — the preview just won't load.
SUPERSET_EMBED_REGISTER = os.getenv("SUPERSET_EMBED_REGISTER", "true").lower() not in ("0", "false", "no")
# Domains allowed to embed (comma-separated). Empty = allow any domain.
SUPERSET_EMBED_ALLOWED_DOMAINS = [
    d.strip() for d in os.getenv("SUPERSET_EMBED_ALLOWED_DOMAINS", "").split(",") if d.strip()
]

# ── Web UI ────────────────────────────────────────────────────────────
# Sub-path the web UI is served under behind a reverse proxy, e.g. "/chatbot".
# Leave empty when served at the domain root. Used to inject <base> so the
# browser resolves /auth-config, /run and assets under the prefix.
APP_BASE_PATH = os.getenv("APP_BASE_PATH", "").rstrip("/")

# ── MCP Service ───────────────────────────────────────────────────────
MCP_URL = os.getenv("MCP_URL", "https://dashboard.idx-ng.com/mcp")
# Bearer token for the hosted MCP endpoint (sent as `Authorization: Bearer …`).
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
# Must match MCP_DEV_USERNAME set in the Superset Flask config (superset_config.py)
MCP_DEV_USERNAME = os.getenv("MCP_DEV_USERNAME", "admin")

# ── Keycloak (web UI auth) ────────────────────────────────────────────
# Mirrors the ui-cbn Angular client so the same login works across apps.
# KEYCLOAK_ENABLED=false disables auth entirely (local dev only).
KEYCLOAK_ENABLED = os.getenv("KEYCLOAK_ENABLED", "true").lower() not in ("0", "false", "no")
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "https://keycloak.idx-ng.com/auth")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "cbn")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "angular-client")
# Optional: require a specific realm role to use the UI. Empty = any authenticated user.
KEYCLOAK_REQUIRED_ROLE = os.getenv("KEYCLOAK_REQUIRED_ROLE", "")

# ── LLM (OpenAI) ──────────────────────────────────────────────────────
# Defaults target OpenAI's hosted chat-completions API.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_GENERATE_PATH = os.getenv("LLM_GENERATE_PATH", "/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.5")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "600"))
# API key — OPENAI_API_KEY is the conventional name; LLM_API_KEY is also accepted.
LLM_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", "")
# Optional sampling temperature. Leave unset to use the model default
# (some newer models only accept the default); set e.g. LLM_TEMPERATURE=0 to pin it.
_llm_temp = os.getenv("LLM_TEMPERATURE")
LLM_TEMPERATURE = float(_llm_temp) if _llm_temp not in (None, "") else None
# Max output tokens. Reasoning models (gpt-5.x) count internal reasoning toward
# this budget, so keep it generous — too low truncates the JSON plan mid-response
# (finish_reason=length → "Could not parse JSON"). Sent as max_completion_tokens.
# Set to 0 to omit the cap entirely.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))

# ── Pipeline Tuning ───────────────────────────────────────────────────
MAX_CHART_RETRIES = int(os.getenv("MAX_CHART_RETRIES", "3"))
MAX_PLAN_RETRIES = int(os.getenv("MAX_PLAN_RETRIES", "2"))
SQL_PROBE_LIMIT = int(os.getenv("SQL_PROBE_LIMIT", "5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# ── Dataset Domain Context (for LLM system prompt) ───────────────────
DATASET_DOMAIN = """
Nigerian banking transactions dataset (sqllab_agent table).
Columns: tx_id, timestamp, bank_code, bank_name, type (DEPOSIT/WITHDRAWAL),
channel_type (AGENT/COUNTER), location_code, location_type, state, amount,
currency (NGN), status (COMPLETED/FAILED/PENDING), denomination_breakdown,
debtor_account, creditor_account, processing_fee, latitude, longitude,
partition_timestamp, consumer_offset, consumer_partition_no.
Key dimensions: state, bank_name, type, channel_type, status, location_type.
Key measures: amount (NGN currency), processing_fee, COUNT of tx_id.
Time column: timestamp.
""".strip()

# ── Valid Superset Chart Types ────────────────────────────────────────
# ONLY the chart types this Superset MCP's generate_chart actually accepts
# (verified against the live service + schema). The accepted tags are:
#   xy (bar/line/area/scatter), pie, table, pivot_table, big_number,
#   mixed_timeseries, handlebars.
# The MCP REJECTS box_plot / funnel / radar / treemap / sunburst / waterfall, so
# we don't offer them; if one is requested anyway, chart_agent._FAMILY_MAP remaps
# it to the nearest supported family.
VALID_CHART_TYPES = {
    # XY family (echarts) — bar/line/area/scatter, plus stacked/grouped variants
    "bar":          "bar",
    "stacked_bar":  "bar",
    "dist_bar":     "bar",
    "line":         "line",
    "area":         "area",
    "stacked_area": "area",
    "scatter":      "scatter",
    "bubble":       "scatter",
    # Pie family
    "pie":          "pie",
    "donut":        "donut",
    # Table
    "table":        "table",
    # Pivot matrix (also used to render "heatmap" requests)
    "pivot_table":  "pivot_table",
    "heatmap":      "pivot_table",
    # Big number
    "big_number":          "big_number",
    "big_number_total":    "big_number_total",
    # Combo — dual-axis time series (bars + line), via mixed_timeseries
    "combo":            "mixed_timeseries",
    "mixed_timeseries": "mixed_timeseries",
}

# ── MCP filter operators (exactly what Superset MCP accepts) ─────────
# These are the ONLY valid op values for generate_chart filters.
MCP_VALID_OPS = {"=", ">", "<", ">=", "<=", "!=", "LIKE", "ILIKE", "NOT LIKE", "IN", "NOT IN"}