# Architecture — CBN Analytics agent

A chat app that turns a plain-language question into a live Apache Superset
dashboard, shown inline. A React frontend talks to a Flask server, which runs a
multi-agent pipeline that plans, validates, and builds charts entirely through a
hosted **Superset MCP** service, then embeds the result with a guest token.

```
 Browser (React PWA)                 Flask server (agent/pipeline.py)
 ┌───────────────────┐  /auth-config ┌───────────────────────────────┐     ┌──────────────┐
 │ Keycloak login    │──────────────▶│ serves built SPA + JSON/SSE    │     │ Superset MCP │
 │ chat UI           │  /suggestions │  routes                        │────▶│ (meta-tools) │──▶ ClickHouse
 │ SSE stream render │──/run (SSE)──▶│  Pipeline → agents             │     └──────────────┘
 │ embedded SDK      │  /guest-token │  profiler / suggester / LLM    │     ┌──────────────┐
 └───────────────────┘──────────────▶└──────────────────────────────┘────▶│ Luna (Mantle) │
        │  embed iframe (guest token)            │  register_embedding /     └──────────────┘
        └────────────────────────────────────────  mint_guest_token (REST) ─▶ Superset
```

## Frontend (`frontend/`, React + Vite + TypeScript)
- **Boot** (`src/App.tsx`): fetch `/auth-config`, init Keycloak (`src/auth/keycloak.ts`,
  PKCE `login-required`; no-op when `KEYCLOAK_ENABLED=false`), then render the chat.
- **Chat** (`src/components/ChatView.tsx`): fetches `/suggestions` for the welcome
  chips; renders the transcript; auto-scrolls.
- **State** (`src/hooks/usePipeline.ts`): owns the message list, streams `/run`,
  mutates the live assistant message as SSE events arrive, tracks the **active
  dashboard** (sent back as `context` so the next query can be a follow-up), and
  stores per-message **follow-up** suggestions. The context accumulates while the
  user stays on one dashboard: every question asked and every chart's details
  (type, measure, dimension, split, grain, top-N, filters) from `charts_detail`.
- **Message** (`src/components/Message.tsx`): phase chips, collapsible logs, the
  inline `DashboardEmbed` (only the latest message embeds, to avoid many iframes),
  and clickable follow-up chips.
- **Embed** (`src/components/DashboardEmbed.tsx`): `@superset-ui/embedded-sdk` +
  `fetchGuestToken` → inline dashboard. No outbound Superset link.
- **API** (`src/api.ts`): typed `auth-config`, `suggestions`, SSE `runPipeline`
  (async generator), `fetchGuestToken`. Base-path aware via `src/config.ts`
  (`VITE_BASE`), so it works under `/chatbot`.
- **PWA**: `vite-plugin-pwa` (manifest + service worker; CBN-green theme, crest icons).

## Flask server (`agent/pipeline.py :: run_web_server`)
Serves the built SPA (`frontend/dist`) and the API. Routes:
- `GET /` + `/<path>` — the React app and its assets (catch-all; API routes take precedence).
- `GET /auth-config` — Keycloak + embed config for the browser (public).
- `GET /suggestions` — dataset-grounded starter queries (auth, cached).
- `POST /run` — **SSE** stream of pipeline progress; accepts `{query, context?}`.
- `POST /guest-token` — mints a Superset guest token for the inline embed (auth).
- `GET /health`. A WSGI middleware strips the `/chatbot` prefix behind the proxy.
All write/data routes are gated by `@require_auth` (`agent/keycloak_auth.py`,
JWT verified against the realm JWKS).

## The pipeline (`agent/pipeline.py :: Pipeline.run`)
Each `POST /run` builds a fresh `Pipeline` and streams these phases:

1. **Health** — init MCP session + health check.
2. **Intent** (`orchestrator.classify_intent`) — *new dashboard* vs *follow-up*
   (only "follow-up" when an active-dashboard `context` is present). It sees the
   dashboard's earlier questions and charts.
3. **Shortlist** (`orchestrator.shortlist_datasets`) — LLM picks 1-3 candidate
   datasets by name from the live catalog (`dataset_agent.build_catalog`).
4. **Profile** (`agents/profiler.py`) — enrich + profile each candidate via a few
   `execute_sql` probes: row count, per-column cardinality, sample values for
   low-cardinality categoricals, numeric min/max/avg → column roles
   (time/measure/dimension). Cached per dataset (`agent/cache.py`).
5. **Plan** (`orchestrator.generate_plan`) — LLM picks the best candidate and emits
   a chart plan **using the profile** (good dimensions/measures, NL→column mapping
   via sample values, chart type by shape, never aggregate Nullable columns). For
   time series it also sets a **`time_grain`** (ISO 8601 — `PT1M` minute … `P1Y`
   year; `_norm_grain` also accepts plain words), so "errors per minute/day/week/
   month" yields one chart per grain instead of four identical ones. The chart
   count follows the question: one for a specific chart, one per named view, 2–4
   complementary charts for an open question, never more than 6. On a follow-up
   the planner also gets the **current dashboard** (earlier questions + chart
   details), returns only the new charts, resolves "it"/"the same", and turns
   "break it down by X" into the referenced chart split by X.
6. **Refine** (`orchestrator.refine_plan`) — only if the plan used invalid columns
   (safety net, profile-aware).
7. **Validate** (`agents/sql_agent.py`) — run probe SQL per chart; on failure,
   refine once and re-validate.
8. **Keep working** (`Pipeline._keep_working_charts`) — drop charts that can't
   render: probe-failed, or a numeric aggregate on a Nullable column (a verified
   MCP limitation — see Constraints).
9. **Create** (`agents/chart_agent.py`) — build each chart config and call
   `generate_chart` (self-corrects on MCP errors; supports multi-metric series).
   Chart types the MCP can't render (box_plot, histogram, treemap, sunburst, funnel,
   waterfall, gauge, radar, sankey, bubble, real heatmap, smooth line, calendar
   heatmap, country map, geo density) are created via the **Superset REST fallback**
   (`superset_auth.create_chart` — raw `viz_type` + `form_data` + stored
   `query_context`). See **Chart types** below.
    Steps 5–9 run per dataset via `Pipeline._validate_keep_create`. **Dataset
    fallback**: if the chosen dataset produces *no rendered chart* (bad column
    types, broken virtual-dataset SQL, or a create-time error), the pipeline
    retries end-to-end on the next shortlisted candidate until one works.
10. **Assemble** — *new*: `generate_dashboard` + `register_embedding`; *follow-up*:
    `add_chart_to_existing_dashboard` for each new chart on the active dashboard
    (reusing its embed uuid so the inline preview refreshes in place).
11. **Report** — final SSE event with dashboard id/uuid, dataset, chart names, and
    **follow-up suggestions** (`agents/suggester.followup_suggestions`).

### Agents
| Agent | File | Role |
|---|---|---|
| Orchestrator | `agents/orchestrator.py` | intent, shortlist, profile-aware plan, refine, report |
| Dataset | `agents/dataset_agent.py` | catalog (names), enrich (columns + db/schema/sql) |
| Profiler | `agents/profiler.py` | cardinality / samples / ranges → column roles (cached) |
| Suggester | `agents/suggester.py` | starter + follow-up suggestions (LLM, cached) |
| SQL | `agents/sql_agent.py` | probe SQL (validates columns/aggregates before creating) |
| Chart | `agents/chart_agent.py` | per-family Superset chart config + self-correction |
| Dashboard | `agents/dashboard_agent.py` | create dashboard / append charts |

## MCP integration (`agent/mcp_client.py`)
JSON-RPC over streamable-HTTP, Bearer `MCP_AUTH_TOKEN`, args wrapped as
`{"request": {...}}`. The service exposes a **meta-tool registry**: `tools/list`
shows only meta-tools (`search_tools`, `call_tool`); real tools are invoked by
name. Tools used: `list_datasets`, `get_dataset_info`, `execute_sql`,
`generate_chart`, `generate_dashboard`, `add_chart_to_existing_dashboard`,
`get_dashboard_info`, `list_dashboards`, `delete_chart`.

## Auth & embedding
- **App auth**: Keycloak (`angular-client`, realm `cbn`), PKCE in the browser; the
  JWT is verified server-side on every `/run`/`/guest-token`/`/suggestions`.
- **Embedding**: the agent logs into Superset (REST, `SUPERSET_USERNAME/PASSWORD`),
  `register_embedding` makes the dashboard embeddable, and `mint_guest_token`
  issues a short-lived, dashboard-scoped token. Only the public Keycloak client id,
  the user's own token, and the guest token ever reach the browser.

## LLM calls (`agent/llm_client.py`)
The model is GPT-5.6 Luna on Amazon Bedrock Mantle (any OpenAI-compatible
chat-completions endpoint works through `LLM_BASE_URL`/`LLM_MODEL`).
- **Prompts** follow OpenAI's GPT-5.6 guidance: short, outcome-first instructions
  and decision rules, no restated rules or format prose. An A/B eval against the
  earlier rule-heavy prompt got more charts right, 0 follow-up duplicates (was
  1), plans ~30% faster, and half the reasoning tokens.
- **Strict JSON schemas** (Structured Outputs) define every output: the plan
  (`orchestrator.PLAN_SCHEMA`, chart types as an enum, nullable optional fields,
  extra measures in `extra_metrics`), intent, dataset shortlist, and suggestions.
  If an endpoint rejects a schema or `reasoning_effort`, the call is retried once
  in plain JSON mode with the schema written into the prompt.
- **Reasoning effort** per call type: planning and refinement use the model
  default (`LLM_REASONING_EFFORT`); intent, shortlist, and suggestions use
  `LLM_REASONING_EFFORT_FAST` (`low`), which kept 100% accuracy on the eval.

## Caching (`agent/cache.py`)
Module-level TTL cache (survives the per-request `Pipeline`): dataset **profiles**
(by id) and **starter suggestions** (by catalog signature). Keeps repeat queries
fast despite the extra profiling calls.

## Deployment (`infra/`)
Multi-stage Docker: a Node stage builds the frontend with
`VITE_BASE=/chatbot/` (must match `APP_BASE_PATH`); the Python stage serves it +
the API on `:5001`. Behind an HTTPS reverse proxy that routes the `/chatbot/`
subtree to the container.

## Chart types
- **Via the MCP `generate_chart`**: xy (bar/line/area/scatter, +stacked/horizontal/
  grouped), pie/donut, table, pivot_table, big_number, mixed_timeseries (combo),
  handlebars.
- **Via the Superset REST fallback** (`chart_agent` + `superset_auth.create_chart`) —
  the MCP rejects these, so they're created with a raw `viz_type` + `form_data` (+ a
  stored `query_context`, so they render deterministically). Each was verified
  end-to-end against the live Superset (`chart/data` 200 + chart create 201) using
  `form_data` templates lifted from real existing charts:
  - **Distribution / part-of-whole / flow**: box_plot, histogram (→`histogram_v2`),
    treemap (→`treemap_v2`), sunburst (→`sunburst_v2`), funnel, waterfall, gauge
    (→`gauge_chart`), radar, sankey (→`sankey_v2`), bubble (→`bubble_v2`).
  - **Real eCharts heatmap** (→`heatmap_v2`): an `x_axis` × `groupby` (single
    category) matrix of the metric — distinct from the `pivot_table` numeric grid.
  - **smooth_line** (→`echarts_timeseries_smooth`): a curved-line time series.
  - **cal_heatmap**: a metric per day on a month/year calendar grid (needs a
    `time_column`). The viz REQUIRES a bounded `time_range` (Since AND Until) or it
    errors "Please provide both time bounds" — so the builder sets one from the
    plan's date-range filters, falling back to the dataset's actual MIN/MAX of the
    time column (`_data_time_range`, a cheap `/chart/data` probe) so the calendar
    spans the real data instead of unbounded time.
  - **country_map**: a Nigeria choropleth shaded per state/region. The map matches
    regions by **NG-XX ISO_3166-2 codes, not names**, so `chart_agent._region_col`
    auto-picks an ISO-code column (`iso_code`, `stateISO`, …) when present — a names
    column (`"Lagos State"`) renders blank.
  - **deck_screengrid**: a geospatial density map — needs latitude+longitude columns
    (auto-detected by `chart_agent._geo_cols`) and a Mapbox token configured in
    Superset.

  Add a new viz by extending `chart_agent._REST_VIZ` + a branch in `_build_rest_chart`
  (and the chart-type list in `config.VALID_CHART_TYPES` + the planner prompt). For
  geo/temporal viz types, the planner is told to pick them only when the dataset has
  the required columns, and the builders degrade gracefully otherwise.
- **Plan filters apply to REST charts too**: `_build_rest_chart` propagates
  `spec.filters` (date ranges, `status='COMPLETED'`, `amount>1000`, IN-lists) into
  BOTH the stored `query_context` and `form_data.adhoc_filters`, mapping the MCP
  equality op `=` → Superset's `==`. Without this a chart titled e.g. "Jan–Mar 2026"
  would silently render all-time data.

## Known constraints
- **Non-numeric aggregates**: the MCP rejects SUM/AVG/MIN/MAX on ClickHouse
  `Nullable(...)`, `Bool`, and text columns ("non-numeric") with no config
  workaround — such charts are dropped, the planner is told to avoid them, and the
  dataset fallback recovers when possible.
- **`COUNT(*)`**: the MCP rejects metric name `'*'` — count a real column instead
  (handled in `chart_agent`).
- **`delete_dashboard`** errors in the MCP — use Superset's REST `DELETE` instead.
- **Time grain** applies only where a chart has a bucketable time axis: the xy family
  (line/bar/area/scatter), combo, smooth-line, and the real heatmap (temporal
  x-axis). Pure categorical/geo/distribution charts (pie, funnel, treemap, sunburst,
  gauge, radar, sankey, histogram, box_plot, country_map, deck) have no time axis, so
  grain is N/A; cal_heatmap's grain is fixed (one cell per day). The MCP **rejects a
  `time_grain` on pivot_table/table configs** ("An error occurred"), so a temporal
  dimension there isn't bucketed unless the chart is routed through the REST fallback.
- Some pre-existing **virtual datasets have broken SQL** (alias-in-GROUP-BY →
  `NOT_AN_AGGREGATE`); the probe catches these and the agent falls back to a
  sibling dataset.
- The LLM is a reasoning model (GPT-5.6 Luna); a generous `max_completion_tokens`
  prevents truncated plans, and a `finish_reason=length` response is reported
  instead of parsed.
