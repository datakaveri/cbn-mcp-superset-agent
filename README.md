# Superset MCP Agentic Pipeline

A multi-agent system that converts natural-language queries into fully assembled Apache Superset dashboards. The user describes what they want to visualise; the pipeline plans, validates, builds, and publishes charts and a dashboard — automatically.

---

## Architecture Overview

```
User Query (NL)
      │
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 0 · Health Check                                             │
│  Verifies MCP service and Superset API are reachable before running │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 1 · Plan Generation  ──  Orchestrator Agent + LLM            │
│  Translates the query into a structured JSON plan:                  │
│    • Dashboard title                                                │
│    • List of ChartSpecs (type, metric, dimension, filters …)        │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 2 · Dataset Discovery  ──  Dataset Agent + MCP               │
│  Looks up the target dataset in Superset; fetches real column names  │
│  and types.                                                         │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 1b · Plan Refinement  ──  Orchestrator Agent + LLM           │
│  Compares plan columns against actual schema; if mismatches exist   │
│  the LLM corrects column names and re-validates.                    │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 3 · SQL Validation  ──  SQL Agent + MCP                      │
│  Runs lightweight probe queries for each chart to confirm the SQL   │
│  is valid and returns data. Failed probes trigger another LLM       │
│  correction pass before continuing.                                 │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 4 · Chart Creation  ──  Chart Agent + MCP                    │
│  Creates each chart in Superset via the MCP `generate_chart` tool.  │
│  Retries up to MAX_CHART_RETRIES times on failure.                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 5 · Dashboard Assembly  ──  Dashboard Agent + MCP            │
│  Creates a new dashboard, adds all successfully created charts,     │
│  and publishes it. Returns the live dashboard URL.                  │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Phase 6 · Result Reporting  ──  Orchestrator Agent                 │
│  Assembles the final PipelineReport: dashboard URL, chart results,  │
│  SQL previews, and any errors.                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Agents

| Agent | File | Responsibility |
|---|---|---|
| **Orchestrator** | `agents/orchestrator.py` | LLM plan generation, plan refinement, final report |
| **Dataset Agent** | `agents/dataset_agent.py` | Discover datasets and fetch column schemas via MCP |
| **SQL Agent** | `agents/sql_agent.py` | Validate chart SQL with probe queries via MCP |
| **Chart Agent** | `agents/chart_agent.py` | Create charts in Superset via MCP |
| **Dashboard Agent** | `agents/dashboard_agent.py` | Create and publish dashboards via MCP |

### Supporting Modules

| Module | Purpose |
|---|---|
| `pipeline.py` | Sequences all phases; exposes Flask web-server mode |
| `llm_client.py` | HTTP client for the local LLM endpoint |
| `mcp_client.py` | JSON-RPC client for the Superset MCP service |
| `superset_auth.py` | Login / session token management for Superset REST API |
| `models.py` | Shared dataclasses (`AgentResult`, `ChartSpec`, `PipelineReport`, …) |
| `config.py` | All environment-driven configuration knobs |
| `tui.py` | `blessed`-based full-screen terminal UI |
| `main.py` | Entry point (TUI / headless / health-check modes) |

---

## Prerequisites

- Python 3.11+
- A running **Apache Superset** instance with the MCP service enabled
- A running **LLM** endpoint compatible with the Ollama or OpenAI `generate` response format
- The `sqllab_agent` dataset already loaded into Superset (Nigerian banking transactions)

---

## Installation

```bash
# Clone and enter the repo
git clone <repo-url>
cd Superset/agent

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

All settings are read from environment variables with sensible defaults. Either export them in your shell or create a `.env` file and load it before running.

| Variable | Default | Description |
|---|---|---|
| `SUPERSET_BASE_URL` | `http://localhost:9001` | Superset base URL |
| `SUPERSET_USERNAME` | `admin` | Superset login username |
| `SUPERSET_PASSWORD` | `admin` | Superset login password |
| `MCP_URL` | `http://localhost:5008/mcp` | Superset MCP service endpoint |
| `MCP_DEV_USERNAME` | `admin` | Username used by the MCP service (must match `superset_config.py`) |
| `LLM_BASE_URL` | `http://10.10.17.55:80` | Base URL of the LLM server |
| `LLM_GENERATE_PATH` | `/api/generate` | Path to the generate endpoint |
| `LLM_MODEL` | `gpt-20b` | Model name to pass to the LLM |
| `LLM_TIMEOUT` | `600` | LLM request timeout in seconds |
| `MAX_CHART_RETRIES` | `3` | Max retry attempts per chart |
| `MAX_PLAN_RETRIES` | `2` | Max plan refinement passes |
| `SQL_PROBE_LIMIT` | `5` | Rows fetched during SQL validation |
| `REQUEST_TIMEOUT` | `30` | Default HTTP timeout for Superset/MCP requests |

Example `.env`:

```bash
SUPERSET_BASE_URL=http://localhost:9001
SUPERSET_USERNAME=admin
SUPERSET_PASSWORD=admin
MCP_URL=http://localhost:5008/mcp
LLM_BASE_URL=http://10.10.17.55:80
LLM_MODEL=gpt-20b
```

---

## Usage

### Interactive TUI (recommended)

Launches a full-screen terminal dashboard with live pipeline progress, log panel, and results pane.

```bash
python main.py
```

### Headless single query

Runs one query and prints the result to stdout — useful for scripting or quick tests.

```bash
python main.py --query "show deposits vs withdrawals by state"
python main.py --query "top 10 banks by transaction volume" --verbose
```

### Health check only

Verifies MCP service and Superset API connectivity without running a pipeline.

```bash
python main.py --health
```

### Web UI server

Starts a Flask server with a browser-based UI that streams live pipeline progress via SSE.

```bash
python pipeline.py                  # default port 5001
python pipeline.py --port 8080      # custom port
```

Then open `http://localhost:5001` in a browser.

---

## Example Queries

```
show total deposits and withdrawals by state
top 10 banks by transaction volume
heatmap of transaction count by bank and channel type
monthly trends of inflow vs outflow
failed transactions by location type
average processing fee by bank
```

---

## Supported Chart Types

`bar`, `stacked_bar`, `line`, `area`, `stacked_area`, `scatter`, `pie`, `donut`, `table`, `box_plot`, `funnel`, `radar`, `heatmap`, `pivot_table`, `waterfall`, `treemap`, `sunburst`, `big_number`, `big_number_total`

---

## Dataset

The pipeline is pre-configured for the **`sqllab_agent`** table — a Nigerian banking transactions dataset with the following key columns:

| Column | Description |
|---|---|
| `tx_id` | Transaction ID |
| `timestamp` | Transaction timestamp |
| `bank_code` / `bank_name` | Bank identifiers |
| `type` | `DEPOSIT` or `WITHDRAWAL` |
| `channel_type` | `AGENT` or `COUNTER` |
| `state` | Nigerian state |
| `amount` | Transaction amount (NGN) |
| `status` | `COMPLETED`, `FAILED`, or `PENDING` |
| `processing_fee` | Fee charged |
| `latitude` / `longitude` | Transaction location |

To adapt the pipeline to a different dataset, update `DATASET_DOMAIN` in `config.py`.

---

## Logs

All pipeline runs are appended to `agent/agent.log`. Pass `--verbose` for `DEBUG`-level output.
