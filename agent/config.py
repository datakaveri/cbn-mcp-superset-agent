"""
Centralized configuration for the Superset MCP Agentic Pipeline.
All endpoints, credentials, and tuning knobs live here.
Reads from environment variables with defaults matching the local setup.
"""

import os

# ── Superset ──────────────────────────────────────────────────────────
SUPERSET_BASE_URL = os.getenv("SUPERSET_BASE_URL", "http://localhost:9001")
SUPERSET_API_URL = f"{SUPERSET_BASE_URL}/api/v1"
SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "admin")

# ── MCP Service ───────────────────────────────────────────────────────
MCP_URL = os.getenv("MCP_URL", "http://localhost:5008/mcp")
# Must match MCP_DEV_USERNAME set in the Superset Flask config (superset_config.py)
MCP_DEV_USERNAME = os.getenv("MCP_DEV_USERNAME", "admin")

# ── LLM ───────────────────────────────────────────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://10.10.17.55:80")
LLM_GENERATE_PATH = os.getenv("LLM_GENERATE_PATH", "/api/generate")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-20b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "600"))

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
# Maps user-friendly aliases → canonical MCP chart_type string.
# The chart_agent uses this to route to the correct config builder.
# "family" groups: xy (bar/line/area/scatter), pie, table, box_plot,
# funnel, radar, heatmap, waterfall, treemap, sunburst, sankey.
VALID_CHART_TYPES = {
    # XY family — rendered as echarts xy
    "bar":          "bar",
    "stacked_bar":  "bar",
    "line":         "line",
    "area":         "area",
    "stacked_area": "area",
    "scatter":      "scatter",
    "bubble":       "bubble",
    "dist_bar":     "bar",
    # Pie family
    "pie":          "pie",
    "donut":        "donut",
    # Table
    "table":        "table",
    # Box plot
    "box_plot":     "box_plot",
    "boxplot":      "box_plot",
    # Funnel
    "funnel":       "funnel",
    # Radar
    "radar":        "radar",
    # Heatmap — MCP has no native "heatmap" chart_type tag.
    # We send "pivot_table" with color_scheme/conditional_formatting so
    # Superset renders it as a visual heatmap. This is transparent to the user.
    "heatmap":      "pivot_table",
    # Native pivot table
    "pivot_table":  "pivot_table",
    # Waterfall
    "waterfall":    "waterfall",
    # Treemap / sunburst
    "treemap":      "treemap",
    "sunburst":     "sunburst",
    # Big Number
    "big_number":          "big_number",
    "big_number_total":    "big_number_total",
}

# ── MCP filter operators (exactly what Superset MCP accepts) ─────────
# These are the ONLY valid op values for generate_chart filters.
MCP_VALID_OPS = {"=", ">", "<", ">=", "<=", "!=", "LIKE", "ILIKE", "NOT LIKE", "IN", "NOT IN"}