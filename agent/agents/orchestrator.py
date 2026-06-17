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
      "chart_type": "bar|line|pie|table|dist_bar|box_plot|scatter|funnel|radar|heatmap|stacked_bar|area|stacked_area|treemap|sunburst|waterfall|big_number|big_number_total",
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
- metric_column must be a raw column name from the chosen dataset (or "*" for COUNT(*))
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
- For heatmap charts: dimension = the ROW axis, series_column = the COLUMN axis. Always set series_column for heatmaps.
"""

REFINEMENT_SYSTEM_PROMPT = """You are correcting a Superset dashboard plan based on actual dataset schema.

The previous plan had issues. You are given:
1. The original user request
2. The previous plan (which may have wrong column names or filter operators)
3. The actual column names and types from the dataset
4. Any errors from SQL validation

Produce a corrected JSON plan using ONLY columns that exist in the schema.
Respond ONLY with valid JSON, same format as before. No markdown fences.

CRITICAL filter rules:
- op must be one of: =, !=, >, <, >=, <=, LIKE, ILIKE, NOT LIKE, IN, NOT IN
- Use "=" not "==" for equality checks
- val for IN/NOT IN must be a JSON array
- val for numeric comparisons must be a number not a string
- Never use SQL expressions as filter val
"""


class Orchestrator:
    """Generates plans via LLM and handles self-correction."""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def generate_plan(self, user_query: str, catalog: list[DatasetSchema]) -> AgentResult:
        """Phase 1: Parse user intent and plan against the real dataset catalog."""
        log.info("Generating plan for '%s' against %d datasets", user_query[:100], len(catalog))

        user_prompt = (
            "AVAILABLE DATASETS (choose the single most relevant one; use ONLY its columns):\n"
            f"{self._format_catalog(catalog)}\n\n"
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

    def refine_plan(
        self,
        user_query: str,
        previous_plan: PipelinePlan,
        schema: DatasetSchema,
        validation_errors: dict,
    ) -> AgentResult:
        """Phase 1b: Refine a plan based on actual schema and validation errors."""
        columns_info = json.dumps(schema.columns, indent=2)
        plan_json = self._plan_to_json(previous_plan)
        errors_json = json.dumps(validation_errors, indent=2, default=str)

        user_prompt = f"""Original user request: {user_query}

Previous plan:
{plan_json}

Actual dataset columns (name → type):
{columns_info}

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
                charts.append(ChartSpec(
                    name=c.get("name", "Untitled Chart"),
                    chart_type=c.get("chart_type", "bar"),
                    metric=c.get("metric", "COUNT(*)"),
                    metric_column=c.get("metric_column", "*"),
                    aggregate=c.get("aggregate", "COUNT"),
                    dimension=c.get("dimension", ""),
                    time_column=c.get("time_column"),
                    filters=c.get("filters"),
                    stack=bool(c.get("stack", False)),
                    row_limit=c.get("row_limit"),
                    extra_metrics=c.get("extra_metrics"),
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