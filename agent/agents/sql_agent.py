"""
SQL Agent — Phase 3: Column validation via execute_sql probes.

Improvements:
  - row_limit (top-N) is applied via ORDER BY metric DESC LIMIT N in the probe SQL,
    giving accurate preview data that matches what the chart will show.
  - series_column (for grouped/stacked charts) is included in SELECT + GROUP BY.
  - Filters are applied in WHERE so the probe validates filtered data.
  - All filter building is shared with chart_agent via the same op-normalisation
    logic to avoid drift between probe SQL and chart filters.
"""

import logging

from mcp_client import MCPClient
from models import AgentResult, ChartSpec, DatasetSchema
from config import SQL_PROBE_LIMIT

log = logging.getLogger(__name__)

# Same op-normalise map as chart_agent (kept local so sql_agent has no import cycle)
_SQL_OP_MAP: dict[str, str] = {
    "==":       "=",
    "eq":       "=",
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


class SQLAgent:
    """Validates chart specs by running probe SQL queries via MCP."""

    def __init__(self, mcp: MCPClient):
        self.mcp = mcp

    def validate_charts(
        self, charts: list[ChartSpec], schema: DatasetSchema
    ) -> AgentResult:
        """
        Run a probe query for each chart spec to validate columns exist
        and aggregations work.  Returns AgentResult with per-chart results.
        """
        results = {}
        all_valid = True

        for chart in charts:
            probe_result = self._probe_chart(chart, schema)
            results[chart.name] = {
                "valid": probe_result.success,
                "preview": probe_result.data if probe_result.success else None,
                "error": probe_result.error if not probe_result.success else None,
            }
            if not probe_result.success:
                all_valid = False
                log.warning("Probe failed for '%s': %s", chart.name, probe_result.error)
            else:
                row_count = len(probe_result.data) if isinstance(probe_result.data, list) else 0
                log.info("Probe OK for '%s': %d preview rows", chart.name, row_count)

        return AgentResult(
            success=all_valid,
            data=results,
            error=None if all_valid else "Some chart probes failed — see details",
            details=results,
        )

    # ── Internal ──────────────────────────────────────────────────────

    def _probe_chart(self, chart: ChartSpec, schema: DatasetSchema) -> AgentResult:
        """Build and run a probe SQL query for a single ChartSpec."""
        table = schema.table_name or schema.name
        metric_expr = self._build_metric_expr(chart)
        where_clause = self._build_where_clause(chart.filters)

        # Determine the effective row limit for this probe
        # Use chart's row_limit if set, otherwise fall back to SQL_PROBE_LIMIT
        limit = int(chart.row_limit) if chart.row_limit and int(chart.row_limit) > 0 else SQL_PROBE_LIMIT

        # ── Time-series charts ─────────────────────────────────────────
        if chart.time_column and chart.chart_type in (
            "line", "area", "stacked_area", "echarts_timeseries_line"
        ):
            ts_type = schema.columns.get(chart.time_column, "")
            time_col = chart.time_column
            if "VARCHAR" in ts_type.upper() or "TEXT" in ts_type.upper():
                time_col = f"CAST({chart.time_column} AS DATE)"

            select_cols = f"{time_col} AS time_dim, {metric_expr} AS metric_val"
            group_cols = time_col
            order_col = time_col

            # Include series_column if set (e.g. stacked area by type)
            if chart.series_column:
                select_cols = f"{time_col} AS time_dim, {chart.series_column}, {metric_expr} AS metric_val"
                group_cols = f"{time_col}, {chart.series_column}"

            sql = (
                f"SELECT {select_cols} "
                f"FROM {table} "
                f"{where_clause}"
                f"GROUP BY {group_cols} "
                f"ORDER BY {order_col} "
                f"LIMIT {limit}"
            )

        # ── All other chart families ───────────────────────────────────
        else:
            dimension = chart.dimension or "state"
            select_cols = f"{dimension}, {metric_expr} AS metric_val"
            group_cols = dimension

            # For grouped/stacked charts include the series_column
            if chart.series_column and chart.series_column != dimension:
                select_cols = f"{dimension}, {chart.series_column}, {metric_expr} AS metric_val"
                group_cols = f"{dimension}, {chart.series_column}"

            # ORDER BY metric DESC so top-N preview matches chart ordering
            sql = (
                f"SELECT {select_cols} "
                f"FROM {table} "
                f"{where_clause}"
                f"GROUP BY {group_cols} "
                f"ORDER BY metric_val DESC "
                f"LIMIT {limit}"
            )

        log.info("SQL probe for '%s': %s", chart.name, sql)

        result = self.mcp.execute_sql(schema.database_id, sql)
        if not result.success:
            return result

        rows = self._extract_rows(result.data)

        if not rows:
            return AgentResult.fail(
                f"Query returned 0 rows for chart '{chart.name}' — "
                f"check dimension '{chart.dimension}' or metric '{metric_expr}'"
            )

        return AgentResult.ok(rows)

    @staticmethod
    def _extract_rows(data) -> list:
        """Normalise the varied shapes execute_sql can return."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("result", "data", "rows"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return candidate
                if isinstance(candidate, dict):
                    inner = candidate.get("data") or candidate.get("rows")
                    if isinstance(inner, list):
                        return inner
        return []

    @staticmethod
    def _build_where_clause(filters) -> str:
        """
        Build a SQL WHERE clause from the filter list on a ChartSpec.
        Returns empty string if no filters, else "WHERE ... " (trailing space).
        """
        if not filters:
            return ""
        if isinstance(filters, dict):
            filters = [filters]

        clauses = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            col = (
                f.get("col") or f.get("column")
                or f.get("subject") or f.get("field")
            )
            raw_op = str(f.get("op") or f.get("operator") or "=").strip()
            val = (
                f.get("val")        if f.get("val")        is not None else
                f.get("value")      if f.get("value")       is not None else
                f.get("comparator")
            )
            if not col or val is None:
                continue

            # Normalise op to SQL form
            sql_op = _SQL_OP_MAP.get(raw_op.lower(), raw_op.upper())

            if sql_op in ("IN", "NOT IN"):
                if isinstance(val, list):
                    quoted = ", ".join(f"'{v}'" for v in val)
                elif isinstance(val, str):
                    # Try JSON parse
                    try:
                        import json as _j
                        lst = _j.loads(val)
                        quoted = ", ".join(f"'{v}'" for v in lst)
                    except Exception:
                        quoted = ", ".join(f"'{v.strip()}'" for v in val.split(","))
                else:
                    quoted = f"'{val}'"
                clauses.append(f"{col} {sql_op} ({quoted})")
            elif isinstance(val, list):
                # Fallback: treat list as IN
                quoted = ", ".join(f"'{v}'" for v in val)
                clauses.append(f"{col} IN ({quoted})")
            elif isinstance(val, str):
                # Escape single quotes inside string values
                safe_val = val.replace("'", "''")
                clauses.append(f"{col} {sql_op} '{safe_val}'")
            else:
                clauses.append(f"{col} {sql_op} {val}")

        if not clauses:
            return ""
        return "WHERE " + " AND ".join(clauses) + " "

    @staticmethod
    def _build_metric_expr(chart: ChartSpec) -> str:
        """Build a SQL metric expression from a ChartSpec."""
        agg = chart.aggregate.upper()
        col = chart.metric_column

        if agg == "COUNT":
            return f"COUNT({col})"
        elif agg in ("SUM", "AVG", "MIN", "MAX"):
            return f"{agg}({col})"
        else:
            # Raw expression fallback (e.g. LLM produced a CASE expression)
            return chart.metric