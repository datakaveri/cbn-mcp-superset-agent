"""
Chart Agent — Phase 4: Chart creation with self-correction.

Supports ALL Superset eCharts chart families without hardcoding chart-type
lists. The _build_config dispatcher routes by a canonical "family" derived
from the chart_type string; every family has a dedicated config builder.

Families:
  xy        → bar, stacked_bar, line, area, stacked_area, scatter, bubble, dist_bar
  pie       → pie, donut
  table     → table
  box_plot  → box_plot / boxplot
  funnel    → funnel
  radar     → radar
  heatmap   → heatmap
  waterfall → waterfall
  treemap   → treemap
  sunburst  → sunburst
  big_number → big_number, big_number_total

Retries up to MAX_CHART_RETRIES times, applying self-correction on each error.
"""

import copy
import logging
import re

from mcp_client import MCPClient
from models import AgentResult, ChartSpec, ChartResult, DatasetSchema
from config import MAX_CHART_RETRIES, MCP_VALID_OPS

log = logging.getLogger(__name__)

# ── Chart-type → family mapping (drives config builder dispatch) ──────
# Any chart_type not in this map falls through to "xy" as the safe default.
_FAMILY_MAP: dict[str, str] = {
    "bar":              "xy",
    "stacked_bar":      "xy",
    "line":             "xy",
    "area":             "xy",
    "stacked_area":     "xy",
    "scatter":          "xy",
    "bubble":           "xy",
    "dist_bar":         "xy",
    "pie":              "pie",
    "donut":            "pie",
    "table":            "table",
    "box_plot":         "box_plot",
    "boxplot":          "box_plot",
    "funnel":           "funnel",
    "radar":            "radar",
    # MCP has no native heatmap chart_type tag.
    # pivot_table is the correct MCP type; we set color_scheme + conditional
    # formatting in the config so Superset renders it as a visual heatmap.
    "heatmap":          "pivot_table",
    "waterfall":        "waterfall",
    "treemap":          "treemap",
    "sunburst":         "sunburst",
    "big_number":       "big_number",
    "big_number_total": "big_number",
}

# Maps MCP xy config "kind" values from chart_type
_XY_KIND_MAP: dict[str, str] = {
    "bar":          "bar",
    "stacked_bar":  "bar",
    "dist_bar":     "bar",
    "line":         "line",
    "area":         "area",
    "stacked_area": "area",
    "scatter":      "scatter",
    "bubble":       "scatter",  # bubble uses scatter kind + size metric
}

# ── Operator normalisation ────────────────────────────────────────────
# Maps anything the LLM might produce → what Superset MCP actually accepts.
# MCP_VALID_OPS = {"=", ">", "<", ">=", "<=", "!=", "LIKE", "ILIKE",
#                  "NOT LIKE", "IN", "NOT IN"}
_OP_NORMALISE: dict[str, str] = {
    "==":       "=",
    "eq":       "=",
    "equals":   "=",
    "ne":       "!=",
    "neq":      "!=",
    "not_eq":   "!=",
    "gt":       ">",
    "gte":      ">=",
    "lt":       "<",
    "lte":      "<=",
    "in":       "IN",
    "not_in":   "NOT IN",
    "nin":      "NOT IN",
    "like":     "LIKE",
    "ilike":    "ILIKE",
    "not like": "NOT LIKE",
    "not_like": "NOT LIKE",
}


class ChartAgent:
    """Creates Superset charts via MCP with self-correction on failures."""

    def __init__(self, mcp: MCPClient):
        self.mcp = mcp

    # ── Public API ────────────────────────────────────────────────────

    def create_charts(
        self, charts: list[ChartSpec], schema: DatasetSchema
    ) -> AgentResult:
        results: list[ChartResult] = []

        for spec in charts:
            chart_result = self._create_single_chart(spec, schema)
            results.append(chart_result)

            if chart_result.success:
                log.info("✅ Chart '%s' created (id=%d)", spec.name, chart_result.chart_id)
            else:
                log.warning("❌ Chart '%s' failed after %d retries: %s",
                            spec.name, chart_result.retries, chart_result.error)

        succeeded = sum(1 for r in results if r.success)
        total = len(results)
        log.info("Charts: %d/%d succeeded", succeeded, total)

        return AgentResult.ok(results, details={
            "succeeded": succeeded,
            "failed": total - succeeded,
            "total": total,
        })

    # ── Single-chart creation with retry loop ─────────────────────────

    def _create_single_chart(self, spec: ChartSpec, schema: DatasetSchema) -> ChartResult:
        result = ChartResult(spec=spec)
        params = self._build_chart_params(spec, schema)

        for attempt in range(1, MAX_CHART_RETRIES + 1):
            result.retries = attempt
            log.info("Creating chart '%s' (attempt %d/%d)", spec.name, attempt, MAX_CHART_RETRIES)
            log.debug("Chart params: %s", str(params)[:800])

            mcp_result = self.mcp.generate_chart(params)

            if mcp_result.success:
                chart_data = mcp_result.data
                chart_id = self._extract_chart_id(chart_data)
                if chart_id:
                    result.chart_id = int(chart_id)
                    result.success = True
                    return result
                else:
                    result.error = f"Chart created but no ID returned: {str(chart_data)[:200]}"
                    return result

            error_msg = mcp_result.error or ""
            result.error = error_msg
            log.warning("Chart error (attempt %d): %s", attempt, error_msg[:400])

            corrected = self._try_correct(params, error_msg, spec, schema)
            if corrected:
                params = corrected
                log.info("Self-corrected params for '%s', retrying…", spec.name)
            else:
                log.warning("No correction possible for '%s', stopping early", spec.name)
                break

        return result

    # ── ID extraction ─────────────────────────────────────────────────

    @staticmethod
    def _extract_chart_id(data) -> int | None:
        if not isinstance(data, dict):
            return None
        for path in [
            lambda d: d.get("id"),
            lambda d: (d.get("result") or {}).get("id"),
            lambda d: (d.get("chart") or {}).get("id"),
        ]:
            val = path(data)
            if val:
                return val
        return None

    # ── Top-level param builder ───────────────────────────────────────

    def _build_chart_params(self, spec: ChartSpec, schema: DatasetSchema) -> dict:
        """Build the full dict passed to mcp.generate_chart()."""
        # COUNT(*): the MCP rejects metric name='*' ("An error occurred"). Count a
        # real column instead — the grouped dimension/time column is always present,
        # so COUNT(<that>) matches COUNT(*) row counts.
        if (spec.aggregate or "").upper() == "COUNT" and (not spec.metric_column or spec.metric_column == "*"):
            spec.metric_column = spec.dimension or spec.time_column or next(iter(schema.columns), "")

        config = self._build_config(spec, schema)

        # Inject filters into config (position depends on family)
        mcp_filters = self._normalise_filters(spec.filters)
        if mcp_filters:
            family = _FAMILY_MAP.get(spec.chart_type.lower(), "xy")
            # XY and box_plot configs nest filters inside the config dict
            config["filters"] = mcp_filters
            log.info("Chart '%s': injecting %d filter(s): %s",
                     spec.name, len(mcp_filters), mcp_filters)

        return {
            "dataset_id": schema.id,
            "chart_name": spec.name,
            "save_chart": True,
            "generate_preview": False,
            "config": config,
        }

    # ── Config builder dispatcher ─────────────────────────────────────

    def _build_config(self, spec: ChartSpec, schema: DatasetSchema) -> dict:
        """Route to the correct config builder based on chart family."""
        family = _FAMILY_MAP.get(spec.chart_type.lower(), "xy")

        dispatch = {
            "xy":           self._xy_config,
            "pie":          self._pie_config,
            "table":        self._table_config,
            "box_plot":     self._box_plot_config,
            "funnel":       self._funnel_config,
            "radar":        self._radar_config,
            "pivot_table":  self._pivot_table_config,   # heatmap routes here
            "waterfall":    self._waterfall_config,
            "treemap":      self._treemap_config,
            "sunburst":     self._sunburst_config,
            "big_number":   self._big_number_config,
        }

        builder = dispatch.get(family, self._xy_config)
        return builder(spec, schema)

    # ── XY family ────────────────────────────────────────────────────

    def _xy_config(self, spec: ChartSpec, schema: DatasetSchema) -> dict:
        """
        Builds an xy config for bar / line / area / scatter / bubble.

        Stacking:
          When spec.stack=True OR chart_type is stacked_bar/stacked_area,
          we set "stack": True inside the config so Superset renders stacked.

        Series splitting (grouped/stacked by a second dimension):
          When spec.series_column is set, we add group_by so Superset
          generates one series per unique value of that column.
          This is how "deposits vs withdrawals" stacked bars work.

        Top-N (row_limit):
          When spec.row_limit is set, we pass "row_limit" in the config.
          Superset/MCP will ORDER BY the metric DESC and limit results.
        """
        ct_lower = spec.chart_type.lower()
        kind = _XY_KIND_MAP.get(ct_lower, "bar")

        is_stacked = spec.stack or ct_lower in ("stacked_bar", "stacked_area")

        # Primary metric + any extra_metrics (multi-metric charts, e.g. inflow +
        # outflow rendered as two series on one xy chart).
        y_metrics = [{
            "name": spec.metric_column,
            "aggregate": spec.aggregate.upper(),
            "label": spec.metric,
        }]
        for m in (spec.extra_metrics or []):
            if not isinstance(m, dict):
                continue
            col = m.get("metric_column") or m.get("name") or m.get("column")
            if not col:
                continue
            m_agg = (m.get("aggregate") or spec.aggregate).upper()
            y_metrics.append({
                "name": col,
                "aggregate": m_agg,
                "label": m.get("label") or f"{m_agg} {col}",
            })

        config: dict = {
            "chart_type": "xy",
            "kind": kind,
            "y": y_metrics,
        }

        if is_stacked:
            config["stack"] = True

        # Determine X axis: temporal vs categorical.
        # Recognize ClickHouse temporal types (DateTime/Date) too, not just TIMESTAMP.
        col_type = schema.columns.get(spec.time_column or "", "")
        is_timestamp = (
            any(t in col_type.upper() for t in ("TIMESTAMP", "DATETIME", "DATE", "TIME"))
            if col_type else False
        )

        if spec.time_column and is_timestamp:
            config["x"] = {
                "name": spec.time_column,
                "dtype": col_type,
            }
            config["time_grain"] = "PT1M"
            log.info("XY chart '%s': time-series x=%s grain=PT1M", spec.name, spec.time_column)

            # series_column drives the group_by (e.g. "type" for DEPOSIT/WITHDRAWAL)
            if spec.series_column and spec.series_column != spec.time_column:
                config["group_by"] = [{"name": spec.series_column}]
            elif spec.dimension and spec.dimension != spec.time_column:
                config["group_by"] = [{"name": spec.dimension}]
        else:
            # Categorical X axis. Never fall back to the table name — use the
            # dimension, else the time column as a last resort.
            x_col = spec.dimension or spec.time_column or ""
            col_dtype = schema.columns.get(x_col, "VARCHAR")
            config["x"] = {
                "name": x_col,
                "dtype": col_dtype,
            }
            log.info("XY chart '%s': categorical x=%s", spec.name, x_col)

            # series_column splits bars/lines (e.g. "type" → DEPOSIT + WITHDRAWAL stacks)
            if spec.series_column and spec.series_column != x_col:
                config["group_by"] = [{"name": spec.series_column}]

        # row_limit — top-N support
        if spec.row_limit and isinstance(spec.row_limit, int) and spec.row_limit > 0:
            config["row_limit"] = spec.row_limit
            log.info("XY chart '%s': row_limit=%d", spec.name, spec.row_limit)

        return config

    # ── Pie / Donut ───────────────────────────────────────────────────

    @staticmethod
    def _pie_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        config = {
            "chart_type": "pie",
            "dimension": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
            "donut": spec.chart_type.lower() == "donut",
        }
        if spec.row_limit and isinstance(spec.row_limit, int) and spec.row_limit > 0:
            config["row_limit"] = spec.row_limit
        return config

    # ── Table ─────────────────────────────────────────────────────────

    @staticmethod
    def _table_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        config = {
            "chart_type": "table",
            "columns": [
                {"name": spec.dimension},
                {
                    "name": spec.metric_column,
                    "aggregate": spec.aggregate.upper(),
                    "label": spec.metric,
                },
            ],
        }
        if spec.row_limit and isinstance(spec.row_limit, int) and spec.row_limit > 0:
            config["row_limit"] = spec.row_limit
        return config

    # ── Box Plot ──────────────────────────────────────────────────────

    @staticmethod
    def _box_plot_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        """
        Box plot config.
        dimension → categorical X axis (group by)
        metric_column → the numeric column whose distribution is visualised
        """
        return {
            "chart_type": "box_plot",
            "x": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Funnel ────────────────────────────────────────────────────────

    @staticmethod
    def _funnel_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        return {
            "chart_type": "funnel",
            "dimension": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Radar ─────────────────────────────────────────────────────────

    @staticmethod
    def _radar_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        return {
            "chart_type": "radar",
            "dimension": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Pivot Table / Heatmap ─────────────────────────────────────────
    #
    # MCP's generate_chart does NOT accept chart_type "heatmap" — the valid tags
    # are: xy, table, pie, pivot_table, mixed_timeseries. We render a "heatmap" as
    # a pivot_table (rows × columns matrix of the metric).
    #
    # IMPORTANT: the MCP pivot_table config accepts ONLY rows / columns / metrics /
    # row_limit / show_*_totals / *_format / transpose. It does NOT accept
    # color_scheme or conditional_formatting — sending those fails the chart with a
    # generic "An error occurred" (verified live). So we keep the config minimal.

    @staticmethod
    def _pivot_table_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        """
        Builds a pivot_table config (used for heatmap requests too).
        - rows    = spec.dimension     (e.g. bank_name)
        - columns = spec.series_column (e.g. channel_type); falls back to 'channel_type'
        - metrics = the cell value
        """
        row_col = spec.dimension or "bank_name"
        col_col = spec.series_column or "channel_type"

        config: dict = {
            "chart_type": "pivot_table",
            "rows": [{"name": row_col}],
            "columns": [{"name": col_col}],
            "metrics": [
                {
                    "name": spec.metric_column,
                    "aggregate": spec.aggregate.upper(),
                    "label": spec.metric,
                }
            ],
            "show_row_totals": True,
            "show_column_totals": True,
        }

        if spec.row_limit and isinstance(spec.row_limit, int) and spec.row_limit > 0:
            config["row_limit"] = spec.row_limit

        log.info(
            "Pivot/heatmap chart '%s': rows=%s, columns=%s, metric=%s",
            spec.name, row_col, col_col, spec.metric,
        )
        return config

    # ── Waterfall ─────────────────────────────────────────────────────

    @staticmethod
    def _waterfall_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        return {
            "chart_type": "waterfall",
            "x": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Treemap ───────────────────────────────────────────────────────

    @staticmethod
    def _treemap_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        return {
            "chart_type": "treemap",
            "dimension": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Sunburst ──────────────────────────────────────────────────────

    @staticmethod
    def _sunburst_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        return {
            "chart_type": "sunburst",
            "dimension": {"name": spec.dimension},
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Big Number ────────────────────────────────────────────────────

    @staticmethod
    def _big_number_config(spec: ChartSpec, schema: DatasetSchema) -> dict:
        # This MCP only accepts "big_number" (not "big_number_total").
        return {
            "chart_type": "big_number",
            "metric": {
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric,
            },
        }

    # ── Filter normalisation ──────────────────────────────────────────

    @staticmethod
    def _normalise_filters(filters) -> list[dict]:
        """
        Convert any filter list the LLM might produce into the exact format
        Superset MCP accepts:
            [{"col": "status", "op": "=", "val": "COMPLETED"}, ...]

        Handles:
          - op aliases (==, eq, gt, gte, in, nin, like, …)
          - numeric coercion for range ops (str "1000" → int 1000)
          - list coercion for IN / NOT IN  (str → list)
          - drops any filter whose op is not in MCP_VALID_OPS after normalisation
        """
        if not filters:
            return []
        if isinstance(filters, dict):
            filters = [filters]

        result = []
        for f in filters:
            if not isinstance(f, dict):
                continue

            col = (
                f.get("col")
                or f.get("column")
                or f.get("subject")
                or f.get("field")
            )
            raw_op = str(
                f.get("op")
                or f.get("operator")
                or "="
            ).strip()
            val = (
                f.get("val")       if f.get("val")       is not None else
                f.get("value")     if f.get("value")      is not None else
                f.get("comparator")
            )

            if not col or val is None:
                log.debug("Filter skipped — missing col or val: %s", f)
                continue

            # Normalise operator
            op = _OP_NORMALISE.get(raw_op.lower(), raw_op)
            # Preserve case for multi-word ops like "NOT IN", "NOT LIKE"
            if op not in MCP_VALID_OPS:
                # Try upper-case
                op_upper = raw_op.upper()
                if op_upper in MCP_VALID_OPS:
                    op = op_upper
                else:
                    log.warning("Filter op '%s' not valid for MCP — skipping filter col='%s'", raw_op, col)
                    continue

            # Coerce val for IN / NOT IN → must be a list
            if op in ("IN", "NOT IN"):
                if isinstance(val, str):
                    # Try JSON parse first, then comma-split
                    try:
                        import json as _json
                        val = _json.loads(val)
                    except Exception:
                        val = [v.strip().strip("'\"") for v in val.split(",") if v.strip()]
                if not isinstance(val, list):
                    val = [val]

            # Coerce val for numeric range ops → must be a number
            elif op in (">", ">=", "<", "<="):
                if isinstance(val, str):
                    try:
                        val = float(val) if "." in val else int(val)
                    except ValueError:
                        pass  # leave as string; MCP will reject if wrong

            result.append({"col": col, "op": op, "val": val})

        return result

    # ── Self-correction ───────────────────────────────────────────────

    @staticmethod
    def _try_correct(
        params: dict,
        error_msg: str,
        spec: ChartSpec,
        schema: DatasetSchema,
    ) -> dict | None:
        """
        Parse MCP error messages and patch params accordingly.
        Returns a corrected params dict, or None if no correction is possible.
        Always deep-copies so previous state is preserved.
        """
        error_lower = error_msg.lower()
        config = copy.deepcopy(params.get("config", {}))
        changed = False

        # ── "did you mean 'X'?" — field name typo correction ──────────
        if "did you mean" in error_lower:
            match = re.search(r"did you mean ['\"]?(\w+)['\"]?", error_lower)
            if match:
                suggestion = match.group(1)
                # "filters" suggestion means the key location is wrong, not a column
                if suggestion != "filters":
                    if isinstance(config.get("x"), dict):
                        config["x"]["name"] = suggestion
                        log.info("Self-correct: x column → '%s'", suggestion)
                        changed = True
                    elif isinstance(config.get("dimension"), dict):
                        config["dimension"]["name"] = suggestion
                        log.info("Self-correct: dimension → '%s'", suggestion)
                        changed = True

        # ── Filter op validation error ─────────────────────────────────
        # Error: "Input should be '=', '>', '<', ... or 'NOT IN'"
        if "input should be" in error_lower and "op" in error_lower:
            existing_filters = config.get("filters", [])
            fixed_filters = []
            for f in existing_filters:
                op = f.get("op", "")
                if op == "==":
                    f = {**f, "op": "="}
                    log.info("Self-correct: filter op '==' → '='")
                # Drop any op that MCP won't accept
                if f.get("op") in MCP_VALID_OPS:
                    fixed_filters.append(f)
                else:
                    log.warning("Self-correct: dropping unsupported filter op '%s'", f.get("op"))
            config["filters"] = fixed_filters
            changed = True

        # ── Wrong chart kind ───────────────────────────────────────────
        if not changed and ("kind" in error_lower or "chart_type" in error_lower):
            # If MCP rejected "heatmap" as chart_type, switch to pivot_table
            if config.get("chart_type") == "heatmap":
                config["chart_type"] = "pivot_table"
                # Restructure from heatmap shape to pivot_table shape
                x = config.pop("x", {})
                y = config.pop("y", {})
                metric = config.pop("metric", {})
                config["rows"] = [x] if x else [{"name": "bank_name"}]
                config["columns"] = [y] if y else [{"name": "channel_type"}]
                config["metrics"] = [metric] if metric else []
                log.info("Self-correct: heatmap → pivot_table (MCP doesn't support heatmap tag)")
                changed = True
            elif config.get("kind") not in ("bar", "line", "area", "scatter"):
                config["kind"] = "bar"
                log.info("Self-correct: kind → 'bar'")
                changed = True

        # ── conditional_formatting not accepted — strip it ─────────────
        if not changed and "conditional_formatting" in error_lower:
            config.pop("conditional_formatting", None)
            config.pop("color_scheme", None)
            log.info("Self-correct: removed conditional_formatting/color_scheme from pivot_table config")
            changed = True

        # ── Bad aggregate / metric ─────────────────────────────────────
        if not changed and ("aggregate" in error_lower or "metric" in error_lower):
            y = config.get("y", [])
            if y:
                y[0] = {**y[0], "aggregate": "COUNT"}
                config["y"] = y
                log.info("Self-correct: aggregate → COUNT")
                changed = True

        # ── stack not valid for this chart type — remove it ───────────
        if "stack" in error_lower and "stack" in config:
            del config["stack"]
            log.info("Self-correct: removed 'stack' from config")
            changed = True

        # ── row_limit not accepted — remove it ────────────────────────
        if "row_limit" in error_lower and "row_limit" in config:
            del config["row_limit"]
            log.info("Self-correct: removed 'row_limit' from config")
            changed = True

        if not changed:
            return None

        return {**params, "config": config}