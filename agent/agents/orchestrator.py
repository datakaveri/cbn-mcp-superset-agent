"""
Orchestrator Agent — Phase 1 & 6: Intent parsing, plan generation, and result reporting.
The brain of the pipeline — calls the LLM to generate plans and handles self-correction.
"""

import json
import logging
from typing import Optional

from llm_client import LLMClient, LLMError
from models import (
    AgentResult, ChartSpec, PipelinePlan, DatasetSchema, PipelineReport,
    ChartResult,
)
from config import VALID_CHART_TYPES

log = logging.getLogger(__name__)

# ── System prompts ────────────────────────────────────────────────────

PLAN_SYSTEM_PROMPT = f"""You are a Superset dashboard planning agent. You are given a user's natural-language request and a catalog of the datasets that exist in Superset (with their REAL column names and types). You produce a JSON plan for creating charts and a dashboard.

RULES:
1. Respond ONLY with valid JSON — no text before or after, no markdown fences.
2. From the AVAILABLE DATASETS, choose the SINGLE most relevant dataset for the request. Set "datasets" to exactly that dataset's table name.
3. Use ONLY column names that exist in the chosen dataset (case-sensitive). Never invent columns.
4. Valid chart_type values: {', '.join(VALID_CHART_TYPES.keys())}
5. For metric, use standard SQL aggregations on real columns (e.g. SUM(<numeric_col>), COUNT(*), AVG(<numeric_col>)).
6. Always include at least one chart. If the request doesn't map cleanly to the data, pick the closest sensible columns from the chosen dataset.

RESPONSE FORMAT:
{{
  "datasets": ["<chosen_table_name>"],
  "dashboard_title": "descriptive title",
  "charts": [
    {{
      "name": "chart display name",
      "chart_type": "bar|stacked_bar|line|area|scatter|pie|donut|table|pivot_table|heatmap|big_number|big_number_total|combo",
      "metric": "SUM(<numeric_col>)",
      "metric_column": "<numeric_col>",
      "aggregate": "SUM",
      "dimension": "<group_by_col>",
      "time_column": null,
      "stack": false,
      "row_limit": null,
      "series_column": null,
      "filters": [
        {{"col": "<col>", "op": "=", "val": "<value>"}}
      ]
    }}
  ]
}}

IMPORTANT:
- USE THE PROFILE (when given per dataset — column role, distinct count, sample values,
  numeric range) to choose well:
  * dimension = a low-cardinality column (role=dimension, small distinct count)
  * metric_column = a numeric column (role=measure); time_column = a column with role=time
  * map words in the request to columns via sample values (e.g. "deposits" →
    filter type='CUSTOMER_DEPOSIT'; "inflow"→deposit, "outflow"→withdrawal)
  * NEVER apply SUM/AVG/MIN/MAX to a NULLABLE, BOOL, or text column — Superset
    rejects it. For a rate over a boolean/flag (e.g. "approval rate"), do NOT
    AVG the flag; instead COUNT with a filter (e.g. metric=COUNT(*), filter
    approved=true) or pick a numeric column / COUNT
  * pick chart_type from the data shape: time→line/area, few categories→bar/pie,
    many categories→table with row_limit (top-N), two dimensions→heatmap
- metric_column must be a raw column name from the chosen dataset (or "*" for COUNT(*))
- Multiple measures / comparisons: when the request combines or compares measures
  (e.g. "inflow and outflow", "X vs Y", "deposits and withdrawals"), set
  "metric_column" to a LIST of the relevant columns (e.g. ["total_in","total_out"])
  and "metric" to a matching list — the chart then renders one series per measure.
- aggregate must be the SQL function name (e.g. "SUM", "COUNT", "AVG", "MIN", "MAX")
- time_column should be a real temporal/timestamp column, set only for time-series charts (line, area)
- dimension is the primary GROUP BY column (shown on X axis or as slices)
- series_column: set this to split bars/lines by a second dimension (grouped/stacked series)
- stack: set true for stacked_bar or stacked_area charts
- row_limit: integer to limit results (e.g. 10 for "top 10"). When set, SQL will ORDER BY metric DESC LIMIT N before charting.
- filters use MCP format — valid ops: =, !=, >, <, >=, <=, LIKE, ILIKE, NOT LIKE, IN, NOT IN
  * Use "=" (not "==") for equality
  * val for IN/NOT IN must be a JSON array: ["val1", "val2"]
  * val for numeric comparisons must be a number, not a string
- Only filter on columns that actually exist in the chosen dataset
- For "top N" requests, set row_limit=N and do NOT add a filter for it
- For heatmap charts: dimension = the ROW axis, series_column = the COLUMN axis. Always set series_column for heatmaps. (Rendered as a pivot matrix.)
- Use "combo" for a dual-axis time series comparing two measures of different scales
  (e.g. transaction COUNT as bars + AVG amount as a line): set time_column, the
  primary metric, and one extra metric in extra_metrics.
- Only these chart_type values are supported — do NOT use box_plot, funnel, radar,
  treemap, sunburst, or waterfall (the backend cannot render them).
"""

REFINEMENT_SYSTEM_PROMPT = """You are correcting a Superset dashboard plan based on actual dataset schema.

The previous plan had issues. You are given:
1. The original user request
2. The previous plan (which may have wrong column names or filter operators)
3. The actual column names and types from the dataset
4. Any errors from SQL validation

Produce a corrected JSON plan using ONLY columns that exist in the schema.
Respond ONLY with valid JSON, same format as before. No markdown fences.

If the user's request involves multiple measures or a comparison (e.g. "inflow
and outflow", "X vs Y", "deposits and withdrawals"), include ALL of them: set
"metric_column" to a LIST of the matching columns (one series each) — map each
measure to the closest column (e.g. inflow → total_in, outflow → total_out).

CRITICAL filter rules:
- op must be one of: =, !=, >, <, >=, <=, LIKE, ILIKE, NOT LIKE, IN, NOT IN
- Use "=" not "==" for equality checks
- val for IN/NOT IN must be a JSON array
- val for numeric comparisons must be a number not a string
- Never use SQL expressions as filter val
"""

SHORTLIST_SYSTEM_PROMPT = """You pick the datasets most relevant to a user's analytics
request. Given dataset table names and the request, return the 3 most relevant table
names, best first (always return at least 2 when plausible, so a fallback exists if
the top choice doesn't work). Respond ONLY with JSON: {"datasets": ["table_name", ...]}"""

INTENT_SYSTEM_PROMPT = """Classify whether a user's request should CREATE a new dashboard
or ADD to / modify the dashboard they're currently viewing.
Reply "followup" only when it clearly extends the current dashboard (e.g. "also show…",
"add a…", "break that down by…", "include…", "on this/that dashboard"). A request that
names a different subject or dataset is "new". When unsure, answer "new".
Respond ONLY with JSON: {"intent": "new"}  or  {"intent": "followup"}"""


class Orchestrator:
    """Generates plans via LLM and handles self-correction."""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def shortlist_datasets(self, user_query: str, catalog: list[DatasetSchema], k: int = 3) -> AgentResult:
        """Pick the 1-k most relevant dataset table names (cheap, names-only)."""
        names = [c.table_name for c in catalog]
        user_prompt = (
            "DATASET NAMES:\n" + "\n".join(f"- {n}" for n in names) +
            f"\n\nUSER REQUEST:\n{user_query}\n\nReturn up to {k} most relevant table names."
        )
        try:
            data = self.llm.generate_json(SHORTLIST_SYSTEM_PROMPT, user_prompt)
        except LLMError as e:
            return AgentResult.fail(f"Dataset shortlist failed: {e}")
        picks = data.get("datasets") if isinstance(data, dict) else data
        picks = [str(p).strip() for p in picks if str(p).strip()][:k] if isinstance(picks, list) else []
        return AgentResult.ok(picks)

    def classify_intent(self, user_query: str, context: Optional[dict]) -> str:
        """Return "new" or "followup". Only "followup" when an active dashboard exists."""
        if not context or not context.get("dashboard_id"):
            return "new"
        user_prompt = (
            f'Current dashboard: "{context.get("title", "")}" '
            f'(dataset: {context.get("dataset", "?")}, '
            f'charts: {context.get("chart_names", [])})\n'
            f"New request: {user_query}"
        )
        try:
            data = self.llm.generate_json(INTENT_SYSTEM_PROMPT, user_prompt)
        except LLMError:
            return "new"
        intent = (data.get("intent") if isinstance(data, dict) else "new") or "new"
        return "followup" if str(intent).lower().strip() == "followup" else "new"

    def generate_plan(self, user_query: str, candidates: list[DatasetSchema],
                      profiles: Optional[dict] = None) -> AgentResult:
        """Phase 1: Plan against enriched + profiled candidate datasets."""
        log.info("Generating plan for '%s' over %d candidate dataset(s)",
                 user_query[:100], len(candidates))

        user_prompt = (
            "CANDIDATE DATASETS (choose the single best one; use ONLY its columns):\n"
            f"{self._format_candidates(candidates, profiles)}\n\n"
            f"USER REQUEST:\n{user_query}"
        )

        try:
            data = self.llm.generate_json(PLAN_SYSTEM_PROMPT, user_prompt)
        except LLMError as e:
            return AgentResult.fail(f"LLM plan generation failed: {e}")

        plan = self._parse_plan(data)
        if plan is None:
            return AgentResult.fail(f"Could not parse plan from LLM response: {data}")

        log.info("Plan generated: %d charts, dataset=%s, dashboard='%s'",
                 len(plan.charts), plan.datasets, plan.dashboard_title)
        return AgentResult.ok(plan)

    @staticmethod
    def _format_catalog(catalog: list[DatasetSchema]) -> str:
        """Render the dataset catalog (table name + columns) for the LLM prompt."""
        lines = []
        for s in catalog:
            if s.columns:
                cols = ", ".join(f"{n} ({t})" for n, t in list(s.columns.items())[:40])
            else:
                cols = "(columns unavailable)"
            lines.append(f'- "{s.table_name}": {cols}')
        return "\n".join(lines) if lines else "(no datasets available)"

    @staticmethod
    def _format_candidates(candidates: list[DatasetSchema], profiles: Optional[dict]) -> str:
        """Render candidate datasets with columns + profile for profile-aware planning."""
        blocks = []
        for s in candidates:
            cols = (", ".join(f"{n} ({t})" for n, t in list(s.columns.items())[:40])
                    if s.columns else "(columns unavailable)")
            block = [f'DATASET "{s.table_name}":', f"  columns: {cols}"]
            prof = (profiles or {}).get(s.table_name)
            if prof is not None:
                block.append("  profile:")
                block.append("\n".join("  " + ln for ln in prof.render().splitlines()))
            blocks.append("\n".join(block))
        return "\n\n".join(blocks) if blocks else "(no datasets available)"

    def refine_plan(
        self,
        user_query: str,
        previous_plan: PipelinePlan,
        schema: DatasetSchema,
        validation_errors: dict,
        profile_text: str = "",
    ) -> AgentResult:
        """Phase 1b: Refine a plan based on actual schema and validation errors."""
        columns_info = json.dumps(schema.columns, indent=2)
        plan_json = self._plan_to_json(previous_plan)
        errors_json = json.dumps(validation_errors, indent=2, default=str)
        profile_block = f"\nDataset profile:\n{profile_text}\n" if profile_text else ""

        user_prompt = f"""Original user request: {user_query}

Previous plan:
{plan_json}

Actual dataset columns (name → type):
{columns_info}
{profile_block}
SQL validation errors:
{errors_json}

Please fix the plan to use only valid column names and correct any issues."""

        try:
            data = self.llm.generate_json(REFINEMENT_SYSTEM_PROMPT, user_prompt)
        except LLMError as e:
            return AgentResult.fail(f"LLM plan refinement failed: {e}")

        plan = self._parse_plan(data)
        if plan is None:
            return AgentResult.fail(f"Could not parse refined plan: {data}")

        log.info("Plan refined: %d charts", len(plan.charts))
        return AgentResult.ok(plan)

    def build_report(
        self,
        dashboard_result: AgentResult,
        chart_results: list[ChartResult],
        sql_previews: dict,
    ) -> PipelineReport:
        """Phase 6: Assemble the final structured report."""
        report = PipelineReport()
        report.charts_created = chart_results
        report.sql_previews = sql_previews

        if dashboard_result.success and isinstance(dashboard_result.data, dict):
            report.dashboard_url = dashboard_result.data.get("url")
            report.dashboard_id = dashboard_result.data.get("dashboard_id")
            report.dashboard_uuid = dashboard_result.data.get("uuid")
            report.success = True

        for cr in chart_results:
            if not cr.success:
                report.errors.append(f"Chart '{cr.spec.name}': {cr.error}")

        if not dashboard_result.success:
            report.errors.append(f"Dashboard: {dashboard_result.error}")

        return report

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_plan(data: dict | list) -> Optional[PipelinePlan]:
        """Parse an LLM response into a PipelinePlan."""
        if not isinstance(data, dict):
            return None

        try:
            charts = []
            for c in data.get("charts", []):
                # The LLM may return metric_column AND metric as parallel LISTS for
                # multi-measure charts (e.g. inflow + outflow). ChartSpec wants a
                # single primary metric, so normalize: first → primary, the rest →
                # extra_metrics. This stops list values leaking downstream into SQL
                # (SUM([...])), schema lookups, OR chart labels (a list label makes
                # the MCP reject the chart with a generic "An error occurred").
                agg = c.get("aggregate", "COUNT")
                mcol = c.get("metric_column", "*")
                mval = c.get("metric", "COUNT(*)")
                labels = [str(x) for x in mval] if isinstance(mval, list) else []
                extra = list(c.get("extra_metrics") or [])
                if isinstance(mcol, list):
                    cols = [str(x) for x in mcol if x]
                    mcol = cols[0] if cols else "*"
                    for i, ec in enumerate(cols[1:], start=1):
                        extra.append({
                            "metric_column": ec, "aggregate": agg,
                            "label": labels[i] if i < len(labels) else f"{agg.title()} {ec}",
                        })
                # The primary metric label must be a single string, never a list.
                metric = labels[0] if labels else (mval if isinstance(mval, str) else "COUNT(*)")
                dim = c.get("dimension", "")
                if isinstance(dim, list):
                    dim = str(dim[0]) if dim else ""
                charts.append(ChartSpec(
                    name=c.get("name", "Untitled Chart"),
                    chart_type=c.get("chart_type", "bar"),
                    metric=metric,
                    metric_column=mcol,
                    aggregate=agg,
                    dimension=dim,
                    time_column=c.get("time_column"),
                    filters=c.get("filters"),
                    stack=bool(c.get("stack", False)),
                    row_limit=c.get("row_limit"),
                    extra_metrics=extra or None,
                    series_column=c.get("series_column"),
                ))

            if not charts:
                return None

            return PipelinePlan(
                datasets=data.get("datasets", []),
                charts=charts,
                dashboard_title=data.get("dashboard_title", "Agent Dashboard"),
            )
        except (KeyError, TypeError) as e:
            log.error("Plan parse error: %s", e)
            return None

    @staticmethod
    def _plan_to_json(plan: PipelinePlan) -> str:
        """Serialize a plan back to JSON for the refinement prompt."""
        return json.dumps({
            "datasets": plan.datasets,
            "dashboard_title": plan.dashboard_title,
            "charts": [
                {
                    "name": c.name,
                    "chart_type": c.chart_type,
                    "metric": c.metric,
                    "metric_column": c.metric_column,
                    "aggregate": c.aggregate,
                    "dimension": c.dimension,
                    "time_column": c.time_column,
                    "stack": c.stack,
                    "row_limit": c.row_limit,
                    "series_column": c.series_column,
                    "filters": c.filters or [],
                }
                for c in plan.charts
            ],
        }, indent=2)