"""
Pipeline — Full Phase 0→6 execution.
Sequences the agents, handles the self-correction loop, and emits progress callbacks.

Also exposes a Flask web server (run_web_server()) that serves the built React
app (frontend/dist) and a POST /run SSE endpoint so the browser UI can stream
live pipeline progress.

Usage:
    python pipeline.py                   # start the web UI on :5001
    python pipeline.py --port 8080       # custom port
"""

import json
import logging
import mimetypes
import os
import queue
import sys
import threading
import time
from datetime import datetime
from typing import Callable, Generator, Optional

from mcp_client import MCPClient, MCPError
from llm_client import LLMClient
from superset_auth import SupersetAuth
from models import (
    AgentResult, Phase, PipelinePlan, PipelineReport,
    DatasetSchema, LogEntry,
)
from agents.orchestrator import Orchestrator
from agents.dataset_agent import DatasetAgent
from agents.sql_agent import SQLAgent
from agents.chart_agent import ChartAgent
from agents.dashboard_agent import DashboardAgent
from agents.profiler import profile_dataset
from agents import suggester
from keycloak_auth import require_auth
from config import (
    MAX_PLAN_RETRIES, APP_BASE_PATH, SUPERSET_LOGIN_ENABLED,
    KEYCLOAK_ENABLED, KEYCLOAK_URL, KEYCLOAK_REALM,
    KEYCLOAK_CLIENT_ID, KEYCLOAK_REQUIRED_ROLE,
    SUPERSET_EMBED_ENABLED, SUPERSET_DOMAIN,
    SUPERSET_EMBED_REGISTER, SUPERSET_EMBED_ALLOWED_DOMAINS,
)

log = logging.getLogger(__name__)

# Type alias for the progress callback
ProgressCallback = Callable[[Phase, str, str], None]  # (phase, level, message)


# ── Pipeline core ────────────────────────────────────────────────────

class Pipeline:
    """
    Orchestrates the full Phase 0→6 pipeline.
    Accepts a progress callback for TUI / SSE / headless integration.
    """

    def __init__(self, on_progress: Optional[ProgressCallback] = None):
        self.mcp = MCPClient()
        self.llm = LLMClient()
        self.auth = SupersetAuth()
        self.on_progress = on_progress or self._default_progress

        # Agents
        self.orchestrator = Orchestrator(self.llm)
        self.dataset_agent = DatasetAgent(self.mcp)
        self.sql_agent = SQLAgent(self.mcp)
        # auth enables the REST fallback for chart types the MCP can't render.
        self.chart_agent = ChartAgent(self.mcp, self.auth)
        self.dashboard_agent = DashboardAgent(self.mcp)

    def _emit(self, phase: Phase, level: str, message: str):
        """Emit a progress event to whatever listener is registered."""
        self.on_progress(phase, level, message)

    @staticmethod
    def _default_progress(phase: Phase, level: str, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"info": "ℹ", "success": "✅", "warning": "⚠", "error": "❌"}.get(level, "•")
        print(f"[{ts}] {prefix} [{phase.value}] {message}")

    # ── Phase 0: Health Check ────────────────────────────────────────

    def health_check(self) -> AgentResult:
        """Phase 0: Verify MCP and Superset connectivity."""
        self._emit(Phase.HEALTH_CHECK, "info", "Starting health checks...")

        try:
            self.mcp.initialize()
            self._emit(Phase.HEALTH_CHECK, "success", "MCP session initialized")
        except MCPError as e:
            self._emit(Phase.HEALTH_CHECK, "error", f"MCP init failed: {e}")
            return AgentResult.fail(f"MCP initialization failed: {e}")

        mcp_result = self.mcp.health_check()
        if not mcp_result.success:
            self._emit(Phase.HEALTH_CHECK, "error", f"MCP health check failed: {mcp_result.error}")
            return AgentResult.fail(f"MCP health check failed: {mcp_result.error}")
        self._emit(Phase.HEALTH_CHECK, "success", "MCP health check passed")

        # Superset REST login is optional: the pipeline operates through MCP and
        # never uses this token. Skip when disabled, and treat failure as a
        # non-fatal warning so an SSO-only Superset doesn't block the run.
        if not SUPERSET_LOGIN_ENABLED:
            self._emit(Phase.HEALTH_CHECK, "info",
                       "Superset REST login skipped (SUPERSET_LOGIN_ENABLED=false); using MCP")
        else:
            auth_result = self.auth.login()
            if auth_result.success:
                self._emit(Phase.HEALTH_CHECK, "success", "Superset login successful")
            else:
                self._emit(Phase.HEALTH_CHECK, "warning",
                           f"Superset login failed (continuing — MCP handles operations): {auth_result.error}")

        return AgentResult.ok({"mcp": "ok", "superset": "ok"})

    # ── Full Pipeline Run ────────────────────────────────────────────

    def run(self, user_query: str, context: Optional[dict] = None) -> PipelineReport:
        """
        Execute the full pipeline: Phase 0 → Phase 6.
        `context` (optional) carries the active dashboard for conversational
        follow-ups: {dashboard_id, dashboard_uuid, dataset, chart_names}.
        Returns a PipelineReport with the final result.
        """
        start = time.time()

        # ── Phase 0: Health Check ──
        health = self.health_check()
        if not health.success:
            report = PipelineReport()
            report.errors.append(f"Health check failed: {health.error}")
            return report

        # ── Phase 1: Plan Generation (reads the live dataset catalog first) ──
        self._emit(Phase.PLAN_GENERATION, "info", "Reading available datasets from Superset...")
        catalog_result = self.dataset_agent.build_catalog()
        if not catalog_result.success:
            self._emit(Phase.PLAN_GENERATION, "error", f"Catalog failed: {catalog_result.error}")
            report = PipelineReport()
            report.errors.append(f"Dataset catalog failed: {catalog_result.error}")
            return report

        catalog: list[DatasetSchema] = catalog_result.data
        preview = ", ".join(c.table_name for c in catalog[:8]) + ("…" if len(catalog) > 8 else "")
        self._emit(Phase.PLAN_GENERATION, "info", f"{len(catalog)} datasets available: {preview}")

        # Intent: brand-new dashboard vs follow-up (add to the active one)
        intent = self.orchestrator.classify_intent(user_query, context)
        if intent == "followup":
            self._emit(Phase.PLAN_GENERATION, "info",
                       "Follow-up detected — will add charts to the current dashboard")

        # Shortlist candidate datasets, then enrich + PROFILE them so the planner
        # chooses good dimensions/metrics/charts instead of a generic COUNT(*).
        shortlist = self.orchestrator.shortlist_datasets(user_query, catalog)
        candidate_names = shortlist.data if shortlist.success else []
        if intent == "followup" and context and context.get("dataset") \
                and context["dataset"] not in candidate_names:
            candidate_names = [context["dataset"]] + candidate_names
        self._emit(Phase.PLAN_GENERATION, "info",
                   f"Candidate datasets: {candidate_names or '(none — using catalog default)'}")

        candidates: list[DatasetSchema] = []
        profiles: dict = {}
        for name in (candidate_names or [])[:3]:
            cand = self.dataset_agent.select([name], catalog)
            if not cand:
                continue
            cand = self.dataset_agent.enrich(cand)
            candidates.append(cand)
            self._emit(Phase.DATASET_DISCOVERY, "info", f"Profiling '{cand.table_name}'…")
            try:
                profiles[cand.table_name] = profile_dataset(cand, self.mcp)
            except Exception as e:  # profiling is best-effort
                log.warning("Profile failed for %s: %s", cand.table_name, e)
        if not candidates:
            candidates = [self.dataset_agent.enrich(catalog[0])]

        self._emit(Phase.PLAN_GENERATION, "info", f"Generating plan from: '{user_query[:80]}...'")
        plan_result = self.orchestrator.generate_plan(user_query, candidates, profiles)
        if not plan_result.success:
            self._emit(Phase.PLAN_GENERATION, "error", f"Plan failed: {plan_result.error}")
            report = PipelineReport()
            report.errors.append(f"Plan generation failed: {plan_result.error}")
            return report

        plan: PipelinePlan = plan_result.data
        self._emit(Phase.PLAN_GENERATION, "success",
                   f"Plan ready: {len(plan.charts)} charts → '{plan.dashboard_title}'")
        for i, c in enumerate(plan.charts):
            self._emit(Phase.PLAN_GENERATION, "info",
                       f"  Chart {i+1}: {c.name} ({c.chart_type}) — {c.metric} by {c.dimension}")

        # ── Phase 2: resolve the chosen dataset (already enriched + profiled) ──
        schema = next((c for c in candidates if c.table_name in plan.datasets), candidates[0])
        plan.datasets = [schema.table_name]
        profile = profiles.get(schema.table_name)
        profile_text = profile.render() if profile else ""
        self._emit(Phase.DATASET_DISCOVERY, "success",
                   f"Using '{schema.name}' (id={schema.id}, db_id={schema.database_id}, "
                   f"{len(schema.columns)} columns)")

        # ── Phases 1b→4 on the chosen dataset (refine → validate → keep → create) ──
        plan, sql_previews, chart_results = self._validate_keep_create(
            user_query, plan, schema, profile_text)

        # ── Dataset fallback (creation-aware) ──
        # If the chosen dataset produced NO rendered chart (bad column types, a
        # broken virtual-dataset SQL, or a create-time error), re-run end-to-end on
        # the other shortlisted candidates — a sibling dataset often works cleanly.
        if not any(c.success for c in chart_results) and len(candidates) > 1:
            for alt in candidates:
                if alt.table_name == schema.table_name:
                    continue
                self._emit(Phase.PLAN_GENERATION, "warning",
                           f"No chart rendered on '{schema.table_name}' — trying '{alt.table_name}'…")
                alt_res = self.orchestrator.generate_plan(user_query, [alt], profiles)
                if not alt_res.success:
                    continue
                alt_prof = profiles.get(alt.table_name)
                alt_text = alt_prof.render() if alt_prof else ""
                a_plan, a_prev, a_results = self._validate_keep_create(
                    user_query, alt_res.data, alt, alt_text)
                if any(c.success for c in a_results):
                    plan, schema, sql_previews, chart_results, profile_text = \
                        a_plan, alt, a_prev, a_results, alt_text
                    self._emit(Phase.DATASET_DISCOVERY, "success",
                               f"Recovered on dataset '{alt.table_name}'")
                    break

        # ── Phase 5: Dashboard Assembly (new dashboard, or append to current) ──
        is_followup = intent == "followup" and bool(context and context.get("dashboard_id"))
        if is_followup:
            self._emit(Phase.DASHBOARD_ASSEMBLY, "info",
                       f"Adding {len(chart_results)} chart(s) to '{context.get('title', 'the current dashboard')}'")
            dash_result = self.dashboard_agent.add_charts(context["dashboard_id"], chart_results)
            if dash_result.success:
                # Reuse the embed uuid the client already has (dashboard is already
                # registered for embedding); the inline preview just re-renders.
                dash_result.data["uuid"] = context.get("dashboard_uuid") or dash_result.data.get("uuid")
                self._emit(Phase.DASHBOARD_ASSEMBLY, "success",
                           f"Added {dash_result.data.get('chart_count', 0)} chart(s) to the dashboard")
            else:
                self._emit(Phase.DASHBOARD_ASSEMBLY, "error", f"Could not update dashboard: {dash_result.error}")
        else:
            self._emit(Phase.DASHBOARD_ASSEMBLY, "info",
                       f"Assembling dashboard: '{plan.dashboard_title}'")
            dash_result = self.dashboard_agent.create_dashboard(plan.dashboard_title, chart_results)
            if dash_result.success:
                url = dash_result.data.get("url", "")
                self._emit(Phase.DASHBOARD_ASSEMBLY, "success", f"Dashboard live → {url}")
                # Register for embedding so the inline guest-token preview resolves.
                if SUPERSET_EMBED_REGISTER:
                    embed_uuid = self.auth.register_embedding(
                        dash_result.data.get("dashboard_id"), SUPERSET_EMBED_ALLOWED_DOMAINS,
                    )
                    if embed_uuid:
                        dash_result.data["uuid"] = embed_uuid
                        self._emit(Phase.DASHBOARD_ASSEMBLY, "info", "Registered dashboard for inline embedding")
                    else:
                        self._emit(Phase.DASHBOARD_ASSEMBLY, "warning",
                                   "Embed registration failed — inline preview may not load (check Superset creds)")
            else:
                self._emit(Phase.DASHBOARD_ASSEMBLY, "error", f"Dashboard failed: {dash_result.error}")

        # ── Phase 6: Result Reporting ──
        elapsed = round(time.time() - start, 1)
        self._emit(Phase.RESULT_REPORTING, "info", f"Pipeline complete in {elapsed}s")

        report = self.orchestrator.build_report(dash_result, chart_results, sql_previews)
        report.dataset = schema.table_name
        # Contextual follow-up suggestions (best-effort) once a dashboard exists.
        if report.success and report.dashboard_id:
            existing = [c.spec.name for c in chart_results if c.success]
            try:
                report.followups = suggester.followup_suggestions(
                    user_query, schema.table_name, profile_text, existing, self.llm)
            except Exception as e:
                log.info("Followup suggestions failed: %s", e)
        charts_ok = sum(1 for c in chart_results if c.success)
        self._emit(
            Phase.RESULT_REPORTING,
            "success" if report.success else "error",
            f"Final: {'SUCCESS' if report.success else 'PARTIAL'} — "
            f"{charts_ok}/{len(chart_results)} charts, {len(report.errors)} errors",
        )

        return report

    # ── Internal helpers ─────────────────────────────────────────────

    def _validate_keep_create(self, user_query, plan, schema, profile_text):
        """
        Refine → SQL-validate (with one correction pass) → keep only working charts
        → create them, for a single dataset. Returns (plan, sql_previews,
        chart_results). Used for the primary dataset and each fallback candidate.
        """
        self._emit(Phase.PLAN_REFINEMENT, "info", "Validating plan columns...")
        plan = self._refine_plan_if_needed(user_query, plan, schema, profile_text)

        self._emit(Phase.SQL_VALIDATION, "info", "Running SQL probe queries...")
        sql_result = self.sql_agent.validate_charts(plan.charts, schema)
        sql_previews = sql_result.data or {}
        if not sql_result.success:
            self._emit(Phase.SQL_VALIDATION, "warning", "Some probes failed — attempting correction...")
            retry = self.orchestrator.refine_plan(user_query, plan, schema, sql_previews, profile_text)
            if retry.success:
                plan = retry.data
                self._emit(Phase.SQL_VALIDATION, "info", "Re-validating corrected plan...")
                sql_previews = self.sql_agent.validate_charts(plan.charts, schema).data or {}

        for chart_name, preview in sql_previews.items():
            if isinstance(preview, dict) and preview.get("valid"):
                rows = preview.get("preview", [])
                count = len(rows) if isinstance(rows, list) else 0
                self._emit(Phase.SQL_VALIDATION, "success", f"  '{chart_name}': {count} preview rows")
            elif isinstance(preview, dict):
                self._emit(Phase.SQL_VALIDATION, "warning",
                           f"  '{chart_name}': {preview.get('error', 'unknown error')}")

        plan.charts = self._keep_working_charts(plan.charts, schema, sql_previews)
        if not plan.charts:
            return plan, sql_previews, []

        self._emit(Phase.CHART_CREATION, "info", f"Creating {len(plan.charts)} charts...")
        chart_results = self.chart_agent.create_charts(plan.charts, schema).data or []
        for cr in chart_results:
            if cr.success:
                self._emit(Phase.CHART_CREATION, "success", f"  '{cr.spec.name}' → chart_id={cr.chart_id}")
            else:
                self._emit(Phase.CHART_CREATION, "error", f"  '{cr.spec.name}' failed: {cr.error}")
        return plan, sql_previews, chart_results

    def _refine_plan_if_needed(
        self, user_query: str, plan: PipelinePlan, schema: DatasetSchema,
        profile_text: str = "",
    ) -> PipelinePlan:
        """Check if plan uses valid columns; refine via LLM if not."""
        valid_cols = set(schema.columns.keys())
        invalid = set()

        for chart in plan.charts:
            if chart.dimension and chart.dimension not in valid_cols:
                invalid.add(chart.dimension)
            if chart.metric_column and chart.metric_column not in valid_cols:
                invalid.add(chart.metric_column)
            if chart.time_column and chart.time_column not in valid_cols:
                invalid.add(chart.time_column)

        if not invalid:
            self._emit(Phase.PLAN_REFINEMENT, "success", "Plan columns are valid — no refinement needed")
            return plan

        self._emit(Phase.PLAN_REFINEMENT, "warning",
                   f"Invalid columns found: {invalid} — refining plan...")

        refined = self.orchestrator.refine_plan(
            user_query, plan, schema,
            {"invalid_columns": list(invalid), "valid_columns": list(valid_cols)},
            profile_text,
        )

        if refined.success:
            self._emit(Phase.PLAN_REFINEMENT, "success", "Plan refined successfully")
            return refined.data
        else:
            self._emit(Phase.PLAN_REFINEMENT, "warning",
                       f"Refinement failed, using original plan: {refined.error}")
            return plan

    # Numeric aggregates the Superset MCP rejects on "non-numeric" columns — which
    # it considers to include ClickHouse Nullable(...) AND Bool AND text types.
    # COUNT / COUNT_DISTINCT are fine on any type. Verified live: there is NO
    # config-level workaround (sql_expression errors in the xy builder; a dtype hint
    # is ignored), so such a chart always fails at creation — drop it up front.
    _NUMERIC_AGGS = {"SUM", "AVG", "MIN", "MAX", "STDDEV", "VAR", "MEDIAN", "PERCENTILE"}
    _NUMERIC_TYPES = ("INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL")

    @classmethod
    def _mcp_can_aggregate(cls, ctype: str) -> bool:
        """True if the MCP will accept SUM/AVG/MIN/MAX on this column type. It
        rejects Nullable(...), Bool, and text types as 'non-numeric'."""
        t = (ctype or "").upper()
        if "NULLABLE" in t or "BOOL" in t:
            return False
        return any(h in t for h in cls._NUMERIC_TYPES)

    def _keep_working_charts(self, charts, schema, sql_previews):
        """
        Filter a chart list down to the ones that will actually render:
          1. Hard rule: a numeric aggregate (SUM/AVG/…) on a column the MCP treats
             as non-numeric (Nullable / Bool / text) — drop unconditionally.
          2. Probe rule: a chart whose probe hit a real DB error will fail at
             creation too → drop it (even if that empties the set; the dataset
             fallback handles recovery). A "0 rows" probe is a possible
             false-negative (empty window) → keep only if nothing else is valid.
        Emits a warning per dropped chart and a summary line.
        """
        # (1) numeric aggregate on a non-aggregatable column → always fails
        keep = []
        for c in charts:
            agg = (getattr(c, "aggregate", "") or "").upper()
            # metric_column is normally a str; be list-safe (multi-metric plans).
            raw_col = getattr(c, "metric_column", "") or ""
            cols = raw_col if isinstance(raw_col, list) else [raw_col]
            bad = next((str(col) for col in cols
                        if agg in self._NUMERIC_AGGS
                        and not self._mcp_can_aggregate(schema.columns.get(str(col), ""))), None)
            if bad:
                self._emit(
                    Phase.SQL_VALIDATION, "warning",
                    f"  Dropping '{c.name}' — {agg} of non-numeric column '{bad}' "
                    f"(type {schema.columns.get(bad, '?')}) is rejected by Superset",
                )
            else:
                keep.append(c)
        charts = keep

        # (2) probe results: separate valid / hard-fail (real error) / soft-fail (0 rows)
        valid = [c for c in charts if (sql_previews.get(c.name) or {}).get("valid")]
        soft = []
        for c in charts:
            if c in valid:
                continue
            err = (sql_previews.get(c.name) or {}).get("error") or "failed validation"
            if "0 rows" in err.lower():
                soft.append(c)
            else:
                self._emit(Phase.SQL_VALIDATION, "warning",
                           f"  Dropping '{c.name}' — query failed, won't render ({err[:140]})")
        charts = valid + (soft if not valid else [])  # keep 0-row charts only if nothing else

        if charts:
            self._emit(Phase.SQL_VALIDATION, "success",
                       f"Keeping {len(charts)} working chart(s)")
        else:
            self._emit(Phase.SQL_VALIDATION, "warning",
                       "No charts can render on this dataset — see warnings above")
        return charts

    def close(self):
        """Clean up resources."""
        self.mcp.close()
        self.llm.close()
        self.auth.close()


# ── Flask web server ─────────────────────────────────────────────────

def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def run_web_server(port: int = 5001, host: str = "0.0.0.0"):
    """
    Start a Flask server that:
      GET  /          → serves the built React app (frontend/dist)
      POST /run       → SSE stream of pipeline progress events
      GET  /health    → quick JSON health probe for the UI
    """
    try:
        from flask import Flask, request, Response, send_from_directory
        from flask_cors import CORS
    except ImportError:
        print("Flask and flask-cors are required. Install with: pip install flask flask-cors")
        sys.exit(1)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    # The built React app (frontend/dist) lives one level up from agent/ and is
    # the UI we serve (built by `pnpm build` / the Docker image).
    frontend_dist = os.path.join(os.path.dirname(base_dir), "frontend", "dist")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app = Flask(__name__, static_folder=None)  # we serve static assets ourselves
    CORS(app)  # Allow cross-origin requests from any port (browser opening index.html elsewhere)

    # Sub-path support: when deployed behind a proxy under e.g. /chatbot, strip
    # the prefix from incoming paths so our routes (/, /run, /auth-config) match
    # whether or not the proxy already stripped it. No-ops at the domain root.
    _wsgi = app.wsgi_app

    def _strip_prefix(environ, start_response):
        prefix = APP_BASE_PATH or environ.get("HTTP_X_FORWARDED_PREFIX", "").rstrip("/")
        path = environ.get("PATH_INFO", "")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            environ["PATH_INFO"] = path[len(prefix):] or "/"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + prefix
        return _wsgi(environ, start_response)

    app.wsgi_app = _strip_prefix

    # Silence Flask's default request logger so our log format stays clean
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.route("/")
    def index():
        # Serve the built React app. Its asset/API URLs are baked with the correct
        # base (VITE_BASE), so no <base> injection is needed.
        dist_index = os.path.join(frontend_dist, "index.html")
        if os.path.exists(dist_index):
            return send_from_directory(frontend_dist, "index.html")
        return Response(
            "Frontend not built — run `pnpm --dir frontend build` "
            "(the Docker image does this automatically).",
            status=503, mimetype="text/plain",
        )

    @app.route("/health")
    def health():
        return {"status": "ok"}

    @app.route("/auth-config")
    def auth_config():
        # Public: lets the browser bootstrap Keycloak with server-driven config.
        return {
            "enabled": KEYCLOAK_ENABLED,
            "url": KEYCLOAK_URL,
            "realm": KEYCLOAK_REALM,
            "clientId": KEYCLOAK_CLIENT_ID,
            "requiredRole": KEYCLOAK_REQUIRED_ROLE or None,
            "embed": {
                "enabled": SUPERSET_EMBED_ENABLED,
                "supersetDomain": SUPERSET_DOMAIN,
                # The agent mints guest tokens itself at POST /guest-token.
            },
        }

    @app.route("/suggestions")
    @require_auth
    def suggestions():
        # Dataset-grounded starter queries for the welcome screen (LLM, cached).
        pipeline = Pipeline()
        try:
            pipeline.mcp.initialize()
            catalog = pipeline.dataset_agent.build_catalog()
            items = (
                suggester.starter_suggestions(catalog.data, pipeline.dataset_agent, pipeline.llm)
                if catalog.success else suggester.FALLBACK_STARTERS
            )
            return {"suggestions": items}
        except Exception as exc:
            log.warning("Suggestions failed: %s", exc)
            return {"suggestions": suggester.FALLBACK_STARTERS}
        finally:
            pipeline.close()

    # Guest token for the inline embedded-dashboard preview. The agent mints it
    # via Superset (admin creds) so the viewer needn't own the dashboard. Gated
    # by @require_auth so only authenticated users can request one.
    _embed_auth = SupersetAuth()

    @app.route("/guest-token", methods=["POST"])
    @require_auth
    def guest_token():
        body = request.get_json(force=True, silent=True) or {}
        uuid = (body.get("uuid") or body.get("dashboard_id") or "").strip()
        if not uuid:
            return {"error": "uuid required"}, 400
        token = _embed_auth.mint_guest_token(uuid)
        if not token:
            reason = _embed_auth.last_error or "unknown error"
            log.warning("Guest-token mint failed for uuid=%s: %s", uuid, reason)
            return {"error": "could not mint guest token", "reason": reason}, 502
        return {"token": token}

    # ── Static assets (built frontend + PWA) ─────────────────────────
    # Serve any file under frontend/dist — JS/CSS bundles, manifest.webmanifest,
    # sw.js, icons, favicon. The exact API routes above take precedence over this
    # catch-all, so /run, /guest-token, etc. are never shadowed.
    @app.route("/<path:filename>")
    def static_files(filename):
        if os.path.isfile(os.path.join(frontend_dist, filename)):
            resp = send_from_directory(frontend_dist, filename)
            if filename.endswith(("sw.js", "registerSW.js")):
                resp.headers["Service-Worker-Allowed"] = "/"
                resp.headers["Cache-Control"] = "no-cache"
            return resp
        return {"error": "not found"}, 404

    @app.route("/run", methods=["POST"])
    @require_auth
    def run_pipeline():
        body = request.get_json(force=True, silent=True) or {}
        user_query = (body.get("query") or "").strip()
        # Optional active-dashboard context for conversational follow-ups.
        context = body.get("context") if isinstance(body.get("context"), dict) else None

        if not user_query:
            return {"error": "query is required"}, 400

        # Use a thread-safe queue so the pipeline thread can push events
        # and the generator (in the request thread) can pull them.
        q: queue.Queue = queue.Queue()
        SENTINEL = object()

        def on_progress(phase: Phase, level: str, message: str):
            q.put({
                "phase": phase.value,
                "level": level,
                "message": message,
            })

        def run_in_thread():
            pipeline = Pipeline(on_progress=on_progress)
            try:
                report = pipeline.run(user_query, context)
                charts_ok = sum(1 for c in report.charts_created if c.success)
                q.put({
                    "done": True,
                    "success": report.success,
                    "dashboard_url": report.dashboard_url,
                    "dashboard_id": report.dashboard_id,
                    "dashboard_uuid": report.dashboard_uuid,
                    "charts": charts_ok,
                    "charts_total": len(report.charts_created),
                    "errors": report.errors,
                    # Context for the next turn (follow-ups) + suggested next questions.
                    "dataset": report.dataset,
                    "chart_names": [c.spec.name for c in report.charts_created if c.success],
                    "followups": report.followups,
                })
            except Exception as exc:
                log.exception("Unhandled pipeline error")
                q.put({
                    "phase": Phase.RESULT_REPORTING.value,
                    "level": "error",
                    "message": f"Fatal pipeline error: {exc}",
                })
                q.put({
                    "done": True,
                    "success": False,
                    "dashboard_url": None,
                    "errors": [str(exc)],
                })
            finally:
                pipeline.close()
                q.put(SENTINEL)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()

        def generate() -> Generator[str, None, None]:
            while True:
                try:
                    item = q.get(timeout=120)  # 2-min max wait per event
                except queue.Empty:
                    # Send a keep-alive comment so the connection doesn't drop
                    yield ": keep-alive\n\n"
                    continue

                if item is SENTINEL:
                    break

                yield _sse_event(item)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
                "Access-Control-Allow-Origin": "*",
            },
        )

    auth_state = (
        f"Keycloak auth ON (realm={KEYCLOAK_REALM}, client={KEYCLOAK_CLIENT_ID})"
        if KEYCLOAK_ENABLED else "Keycloak auth OFF (KEYCLOAK_ENABLED=false)"
    )
    print(f"\n  ◆ Superset Agent UI → http://localhost:{port}")
    print(f"    {auth_state}\n")
    app.run(host=host, port=port, threaded=True, debug=False)


# ── CLI entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler("agent.log", mode="a"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    parser = argparse.ArgumentParser(description="Superset MCP Pipeline — web server or headless")
    parser.add_argument("--port", "-p", type=int, default=5001, help="Web server port (default: 5001)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--query", "-q", type=str, help="Run a single query headless (no web server)")
    args = parser.parse_args()

    if args.query:
        # Headless single-run mode
        pipeline = Pipeline()
        try:
            report = pipeline.run(args.query)
            print("\n" + "=" * 60)
            if report.success:
                print(f"✅ Dashboard: {report.dashboard_url}")
                ok = sum(1 for c in report.charts_created if c.success)
                print(f"   Charts: {ok}/{len(report.charts_created)}")
            else:
                print("❌ Pipeline failed")
            if report.errors:
                for e in report.errors:
                    print(f"   ⚠ {e}")
            print("=" * 60)
        finally:
            pipeline.close()
    else:
        run_web_server(port=args.port, host=args.host)

