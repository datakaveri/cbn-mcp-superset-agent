"""
Orchestrator Agent — Phase 1 & 6: Intent parsing, plan generation, and result reporting.
The brain of the pipeline — calls the LLM to generate plans and handles self-correction.

The prompts follow OpenAI's GPT-5.6 guidance: short, outcome-first instructions,
with the output shape enforced by strict JSON schemas (Structured Outputs) rather
than prose rules.
"""

import json
import logging
from typing import Optional

from llm_client import LLMClient, LLMError
from models import (
    AgentResult, ChartSpec, PipelinePlan, DatasetSchema, PipelineReport,
    ChartResult,
)
from config import (
    VALID_CHART_TYPES, MCP_VALID_OPS, LLM_REASONING_EFFORT, LLM_REASONING_EFFORT_FAST,
)

log = logging.getLogger(__name__)

MAX_CHARTS = 6   # hard cap per plan, whatever the model returns

# ── Output schemas (strict Structured Outputs) ───────────────────────
# Strict mode requires every property to be listed as required; optional values
# are expressed as nullable types.

_AGGREGATES = ["COUNT", "SUM", "AVG", "MIN", "MAX"]
_GRAINS = ["PT1M", "PT1H", "P1D", "P1W", "P1M", "P3M", "P1Y"]


def _nullable(t: str, description: str) -> dict:
    return {"type": [t, "null"], "description": description}


_FILTER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["col", "op", "val"],
    "properties": {
        "col": {"type": "string"},
        "op": {"type": "string", "enum": sorted(MCP_VALID_OPS)},
        "val": {
            "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"},
                      {"type": "array", "items": {"type": ["string", "number"]}}],
            "description": "A number for numeric comparisons, a list for IN / NOT IN, otherwise a string.",
        },
    },
}

_EXTRA_METRIC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["metric_column", "aggregate", "label"],
    "properties": {
        "metric_column": {"type": "string"},
        "aggregate": {"type": "string", "enum": _AGGREGATES},
        "label": {"type": "string"},
    },
}

_CHART_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "chart_type", "metric", "metric_column", "aggregate", "dimension",
                 "time_column", "time_grain", "series_column", "stack", "row_limit",
                 "filters", "extra_metrics"],
    "properties": {
        "name": {"type": "string", "description": "Short chart title for the dashboard."},
        "chart_type": {"type": "string", "enum": list(VALID_CHART_TYPES)},
        "metric": {"type": "string", "description": "Display label for the measure, e.g. SUM(amount) or COUNT(*)."},
        "metric_column": {"type": "string",
                          "description": "Column to aggregate. COUNT works on any column; '*' counts rows."},
        "aggregate": {"type": "string", "enum": _AGGREGATES},
        "dimension": _nullable("string", "Group-by column: x-axis categories, pie slices, heatmap x-axis, "
                                         "map regions. null for single-number and time-only charts."),
        "time_column": _nullable("string", "Temporal column for time-series, calendar and combo charts; otherwise null."),
        "time_grain": {"anyOf": [{"type": "string", "enum": _GRAINS}, {"type": "null"}],
                       "description": "Bucket size for time_column; null when there is no time axis."},
        "series_column": _nullable("string", "Second category: splits bars or lines into series, heatmap y-axis, "
                                             "sankey target, sunburst inner ring. Otherwise null."),
        "stack": {"type": "boolean", "description": "true for stacked bars or areas."},
        "row_limit": _nullable("integer", "N for a top-N chart; otherwise null."),
        "filters": {"type": "array", "items": _FILTER_SCHEMA,
                    "description": "Row filters on the chosen dataset's columns; empty when none."},
        "extra_metrics": {"type": "array", "items": _EXTRA_METRIC_SCHEMA,
                          "description": "Further measures drawn on the same chart, e.g. outflow next to "
                                         "inflow; empty when none."},
    },
}

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["datasets", "dashboard_title", "charts"],
    "properties": {
        "datasets": {"type": "array", "items": {"type": "string"},
                     "description": "The chosen dataset's table name, as a one-item list."},
        "dashboard_title": {"type": "string",
                            "description": "Dashboard title. For a follow-up, a short title for the added charts."},
        "charts": {"type": "array", "items": _CHART_SCHEMA, "description": "The charts, in display order."},
    },
}

_SHORTLIST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["datasets"],
    "properties": {"datasets": {"type": "array", "items": {"type": "string"},
                                "description": "Table names, best first."}},
}

_INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["intent"],
    "properties": {"intent": {"type": "string", "enum": ["new", "followup"]}},
}

# ── System prompts ────────────────────────────────────────────────────

PLAN_SYSTEM_PROMPT = """You plan Apache Superset dashboards for a banking analytics team in Nigeria. You get a request and one or more candidate datasets (real columns, types, and a data profile). Return the charts that best answer the request, all built on one dataset.

How many charts
- A request for one specific chart ("a pie chart of X", "top 10 banks as a bar chart") gets exactly that chart.
- A request that names several views ("per day and per week", "by channel and over time") gets one chart per view.
- An open question ("give me an overview of", "analyze", "how are we doing on", "build a dashboard for") gets 2 to 4 charts that each answer a different part of it, for example a headline number, a trend over time, a breakdown, and a ranking.
- "A few" or "some" more charts means about 3. Never return more than 6.
- Don't return two charts that show the same measure by the same dimension at the same granularity.

Follow-ups
A CURRENT DASHBOARD block means the user is continuing that conversation. Return only the new charts the request asks for; they are added to that dashboard. Read "it", "that", and "the same" as the chart or question they point to (usually the most recent one), and keep its dataset, measure, filters, and row_limit unless the request changes them. "Break it down by X" or "split it by X" means that chart with X added as series_column (a stacked_bar, or a heatmap when both have many categories), not a new chart of X alone. Don't recreate a chart that is already there. The same data at another granularity, dimension, or chart type counts as new.

Choosing columns and chart types
- Use only columns of the chosen dataset, spelled exactly as given.
- Use the profile. Low-cardinality columns make good dimensions, numeric columns are measures, and role=time columns go in time_column. Map words in the request to the data through the sample values; for example "deposits" becomes a filter on the column whose values include DEPOSIT.
- SUM, AVG, MIN, and MAX need a numeric column that isn't NULLABLE. Otherwise use COUNT, which works on any column. For a rate over a yes/no flag, COUNT with a filter on the flag.
- Match the chart type to the data: time series → line, area, or smooth_line; a few categories → bar or pie; many categories → bar or table with row_limit; two categories → heatmap, or stacked_bar with series_column; a single number → big_number_total; a distribution → histogram or box_plot; parts of a whole → treemap or sunburst; a flow between two categories → sankey; Nigerian states → country_map, using an ISO-code column when there is one; latitude and longitude → deck_screengrid; activity by day → cal_heatmap; two measures of different scale over time → combo.
- Several measures on one chart ("inflow and outflow"): the first in metric_column, the rest in extra_metrics.
- time_grain follows the wording: minute PT1M, hour PT1H, day P1D, week P1W, month P1M, quarter P3M, year P1Y. Use P1D when the request doesn't say.
- For "top N", set row_limit to N rather than adding a filter."""

REFINEMENT_SYSTEM_PROMPT = """You fix a Superset chart plan so that it runs. You get the user's request, the previous plan, the dataset's real columns (with a profile when available), and the validation errors. Return the corrected plan: keep what already works and change only what the errors point at, using real column names from this dataset. SUM, AVG, MIN, and MAX need a numeric column that isn't NULLABLE; otherwise use COUNT. Several measures on one chart go in metric_column plus extra_metrics."""

SHORTLIST_SYSTEM_PROMPT = """You pick the datasets most likely to answer an analytics request, from a list of table names. Return up to 3 table names, best first, copied exactly. Include at least 2 when more than one could plausibly work, so there is a fallback."""

INTENT_SYSTEM_PROMPT = """Decide whether a request continues the dashboard the user is looking at ("followup": add charts to it) or starts a new one ("new"). It is a follow-up when it builds on the current dashboard: it refers to it ("it", "that", "the same"), adds a view ("also show", "add", "break it down by", "what about"), or asks for more charts of the same data. A different subject or dataset is "new". When unsure, choose "new"."""


class Orchestrator:
    """Generates plans via LLM and handles self-correction."""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def shortlist_datasets(self, user_query: str, catalog: list[DatasetSchema], k: int = 3) -> AgentResult:
        """Pick the 1-k most relevant dataset table names (cheap, names-only)."""
        names = [c.table_name for c in catalog]
        user_prompt = (
            "DATASET NAMES:\n" + "\n".join(f"- {n}" for n in names) +
            f"\n\nREQUEST:\n{user_query}\n\nReturn up to {k} table names."
        )
        try:
            data = self.llm.generate_json(SHORTLIST_SYSTEM_PROMPT, user_prompt, schema=_SHORTLIST_SCHEMA,
                                          schema_name="dataset_shortlist",
                                          reasoning_effort=LLM_REASONING_EFFORT_FAST or None)
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
            "CURRENT DASHBOARD:\n" + self._format_context(context) +
            f"\n\nNEW REQUEST:\n{user_query}"
        )
        try:
            data = self.llm.generate_json(INTENT_SYSTEM_PROMPT, user_prompt, schema=_INTENT_SCHEMA,
                                          schema_name="intent",
                                          reasoning_effort=LLM_REASONING_EFFORT_FAST or None)
        except LLMError:
            return "new"
        intent = (data.get("intent") if isinstance(data, dict) else "new") or "new"
        return "followup" if str(intent).lower().strip() == "followup" else "new"

    def generate_plan(self, user_query: str, candidates: list[DatasetSchema],
                      profiles: Optional[dict] = None, context: Optional[dict] = None) -> AgentResult:
        """Phase 1: Plan against enriched + profiled candidate datasets. `context` is
        the active dashboard when the request is a follow-up to it."""
        log.info("Generating plan for '%s' over %d candidate dataset(s)%s",
                 user_query[:100], len(candidates), " as a follow-up" if context else "")

        parts = ["CANDIDATE DATASETS (choose one; use only its columns):",
                 self._format_candidates(candidates, profiles)]
        if context:
            parts += ["", "CURRENT DASHBOARD (the request is a follow-up to it):",
                      self._format_context(context)]
        parts += ["", f"REQUEST:\n{user_query}"]

        try:
            data = self.llm.generate_json(PLAN_SYSTEM_PROMPT, "\n".join(parts), schema=PLAN_SCHEMA,
                                          schema_name="dashboard_plan",
                                          reasoning_effort=LLM_REASONING_EFFORT or None)
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

    @staticmethod
    def _format_context(context: dict) -> str:
        """Render the active dashboard (dataset, earlier questions, charts) for the
        planner and the intent classifier. Accepts the older chart_names-only shape."""
        lines = [f"dataset: {context.get('dataset') or '?'}"]
        queries = [q for q in (context.get("queries") or []) if q]
        if queries:
            lines.append("earlier questions:")
            lines += [f"  {i}. {q}" for i, q in enumerate(queries[-8:], 1)]
        charts = context.get("charts") or [{"name": n} for n in (context.get("chart_names") or [])]
        if charts:
            lines.append("charts already on it:")
            for c in charts[-20:]:
                bits = [c.get("chart_type"), c.get("metric")]
                if c.get("dimension"):
                    bits.append(f"by {c['dimension']}")
                if c.get("series_column"):
                    bits.append(f"split by {c['series_column']}")
                if c.get("time_grain"):
                    bits.append(f"grain {c['time_grain']}")
                if c.get("row_limit"):
                    bits.append(f"top {c['row_limit']}")
                for f in c.get("filters") or []:
                    if isinstance(f, dict) and f.get("col"):
                        bits.append(f"where {f['col']} {f.get('op', '=')} {f.get('val')}")
                detail = ", ".join(str(b) for b in bits if b)
                lines.append(f'  - "{c.get("name", "?")}"' + (f": {detail}" if detail else ""))
        return "\n".join(lines)

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

Fix the plan so every chart uses valid columns and aggregates for this dataset."""

        try:
            data = self.llm.generate_json(REFINEMENT_SYSTEM_PROMPT, user_prompt, schema=PLAN_SCHEMA,
                                          schema_name="dashboard_plan",
                                          reasoning_effort=LLM_REASONING_EFFORT or None)
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

    # Time-grain normalisation — accept ISO 8601 durations or plain English words.
    # NOTE: PT1M = 1 MINUTE, P1M = 1 MONTH (a common confusion).
    _VALID_GRAINS = {"PT1S", "PT1M", "PT5M", "PT10M", "PT15M", "PT30M",
                     "PT1H", "P1D", "P1W", "P1M", "P3M", "P1Y"}
    _GRAIN_WORDS = {
        "second": "PT1S", "secondly": "PT1S",
        "minute": "PT1M", "minutely": "PT1M", "min": "PT1M", "per minute": "PT1M",
        "hour": "PT1H", "hourly": "PT1H", "per hour": "PT1H",
        "day": "P1D", "daily": "P1D", "per day": "P1D",
        "week": "P1W", "weekly": "P1W", "per week": "P1W",
        "month": "P1M", "monthly": "P1M", "per month": "P1M",
        "quarter": "P3M", "quarterly": "P3M",
        "year": "P1Y", "yearly": "P1Y", "annual": "P1Y", "annually": "P1Y",
    }

    @classmethod
    def _norm_grain(cls, v) -> Optional[str]:
        """Normalise an LLM time_grain (ISO duration or English word) → ISO, or None."""
        if not v:
            return None
        s = str(v).strip()
        if s.upper() in cls._VALID_GRAINS:
            return s.upper()
        return cls._GRAIN_WORDS.get(s.lower())

    @staticmethod
    def _parse_plan(data: dict | list) -> Optional[PipelinePlan]:
        """Parse an LLM response into a PipelinePlan."""
        if not isinstance(data, dict):
            return None

        try:
            charts = []
            for c in data.get("charts", []):
                # Multi-measure charts should put the first measure in metric_column
                # and the rest in extra_metrics. Older or non-strict responses may
                # instead send metric_column/metric as parallel LISTS (or a list
                # serialized into a string). Normalize to a single primary metric so
                # list values never leak into SQL (SUM([...])), schema lookups, or
                # chart labels (a list label makes the MCP reject the chart).
                agg = c.get("aggregate") or "COUNT"
                if isinstance(agg, list):          # multi-metric plans may send a list
                    agg = str(agg[0]) if agg else "COUNT"
                mcol = c.get("metric_column") or "*"
                if isinstance(mcol, str) and mcol.strip().startswith("["):
                    try:
                        mcol = json.loads(mcol)
                    except ValueError:
                        pass
                mval = c.get("metric") or "COUNT(*)"
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
                dim = c.get("dimension") or ""
                if isinstance(dim, list):
                    dim = str(dim[0]) if dim else ""
                charts.append(ChartSpec(
                    name=c.get("name") or "Untitled Chart",
                    chart_type=c.get("chart_type") or "bar",
                    metric=metric,
                    metric_column=mcol,
                    aggregate=agg,
                    dimension=dim,
                    time_column=c.get("time_column"),
                    time_grain=Orchestrator._norm_grain(c.get("time_grain")),
                    filters=c.get("filters") or None,
                    stack=bool(c.get("stack", False)),
                    row_limit=c.get("row_limit"),
                    extra_metrics=extra or None,
                    series_column=c.get("series_column"),
                ))

            if not charts:
                return None
            if len(charts) > MAX_CHARTS:
                log.warning("Plan had %d charts; keeping the first %d", len(charts), MAX_CHARTS)
                charts = charts[:MAX_CHARTS]

            return PipelinePlan(
                datasets=data.get("datasets", []),
                charts=charts,
                dashboard_title=data.get("dashboard_title") or "Agent Dashboard",
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
                    "time_grain": c.time_grain,
                    "stack": c.stack,
                    "row_limit": c.row_limit,
                    "series_column": c.series_column,
                    "filters": c.filters or [],
                    "extra_metrics": c.extra_metrics or [],
                }
                for c in plan.charts
            ],
        }, indent=2)
