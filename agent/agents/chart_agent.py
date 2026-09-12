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
import datetime
import logging
import re

from mcp_client import MCPClient
from models import AgentResult, ChartSpec, ChartResult, DatasetSchema
from config import MAX_CHART_RETRIES, MCP_VALID_OPS

log = logging.getLogger(__name__)

# ── Chart-type → family mapping (drives config builder dispatch) ──────
# Only families the MCP actually supports. Any chart_type not in this map falls
# through to "xy". Types the MCP REJECTS (box_plot/funnel/radar/waterfall/
# treemap/sunburst) are remapped here to the nearest supported family so a request
# still renders instead of failing.
_FAMILY_MAP: dict[str, str] = {
    # XY family
    "bar":              "xy",
    "stacked_bar":      "xy",
    "dist_bar":         "xy",
    "line":             "xy",
    "area":             "xy",
    "stacked_area":     "xy",
    "scatter":          "xy",
    "bubble":           "xy",
    # Pie family
    "pie":              "pie",
    "donut":            "pie",
    # Table
    "table":            "table",
    # Pivot matrix (also renders "heatmap" requests)
    "pivot_table":      "pivot_table",
    "heatmap":          "pivot_table",
    # Big number
    "big_number":       "big_number",
    "big_number_total": "big_number",
    # Combo — dual-axis time series (bars + line)
    "mixed_timeseries": "mixed",
    "combo":            "mixed",
    # Not natively supported → nearest supported family
    "box_plot":         "xy",
    "boxplot":          "xy",
    "funnel":           "xy",
    "radar":            "xy",
    "waterfall":        "xy",
    "treemap":          "pie",
    "sunburst":         "pie",
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


# Chart types the MCP's generate_chart can't render → created via Superset REST
# (chart_agent._create_rest_chart). Maps our type → the real Superset viz_type.
# All viz_types below were verified end-to-end (chart/data 200 + chart create 201)
# against the live Superset, using form_data templates lifted from real charts.
_REST_VIZ: dict[str, str] = {
    "box_plot":  "box_plot",
    "boxplot":   "box_plot",
    "funnel":    "funnel",
    "treemap":   "treemap_v2",
    "sunburst":  "sunburst_v2",
    "waterfall": "waterfall",
    "gauge":     "gauge_chart",
    "radar":     "radar",
    "sankey":    "sankey_v2",
    "histogram": "histogram_v2",
    "bubble":    "bubble_v2",
    # Real eCharts heatmap (x-axis × category matrix), not the pivot_table proxy.
    "heatmap":      "heatmap_v2",
    "real_heatmap": "heatmap_v2",
    # Choropleth — value shaded per state/region (Nigeria map).
    "country_map":  "country_map",
    "map":          "country_map",
    "choropleth":   "country_map",
    # Calendar heatmap — a metric per day across a month/year grid.
    "cal_heatmap":      "cal_heatmap",
    "calendar_heatmap": "cal_heatmap",
    "calendar":         "cal_heatmap",
    # Smooth-line time series (curved eCharts line).
    "smooth_line":  "echarts_timeseries_smooth",
    "smooth":       "echarts_timeseries_smooth",
    # Geospatial density — points/screengrid on a basemap (needs lat/long columns).
    "deck_screengrid": "deck_screengrid",
    "density_map":     "deck_screengrid",
    "geo_density":     "deck_screengrid",
}


class ChartAgent:
    """Creates Superset charts via MCP, with a REST fallback for unsupported types."""

    def __init__(self, mcp: MCPClient, auth=None):
        self.mcp = mcp
        self.auth = auth   # SupersetAuth — enables the REST fallback for box_plot etc.

    # ── Public API ────────────────────────────────────────────────────

    def create_charts(
        self, charts: list[ChartSpec], schema: DatasetSchema
    ) -> AgentResult:
        results: list[ChartResult] = []

        for spec in charts:
            # Route MCP-unsupported viz types to the Superset REST fallback.
            if self.auth and spec.chart_type.lower() in _REST_VIZ:
                chart_result = self._create_rest_chart(spec, schema)
            else:
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

    # ── REST fallback (chart types the MCP can't render) ──────────────

    def _create_rest_chart(self, spec: ChartSpec, schema: DatasetSchema) -> ChartResult:
        """Create a chart via Superset REST for viz types generate_chart rejects."""
        result = ChartResult(spec=spec, retries=1)
        try:
            viz, form_data, query_context = self._build_rest_chart(spec, schema)
            cid = self.auth.create_chart(spec.name, viz, schema.id, form_data, query_context)
            if cid:
                result.chart_id = int(cid)
                result.success = True
            else:
                result.error = self.auth.last_error or "REST chart create failed"
        except Exception as e:  # never let a builder error abort the run
            result.error = f"REST chart create error: {e}"
        return result

    # ── Column-role helpers (for geo / temporal viz types) ────────────

    @staticmethod
    def _find_col(schema: DatasetSchema, *needles) -> str | None:
        """First column whose name contains any needle (case-insensitive)."""
        for name in schema.columns:
            low = name.lower()
            if any(n in low for n in needles):
                return name
        return None

    @staticmethod
    def _geo_cols(schema: DatasetSchema):
        """(lat, lon) column names if the dataset has them, else (None, None)."""
        lat = (ChartAgent._find_col(schema, "latitude", "gps_lat", "_lat")
               or ChartAgent._find_col(schema, "lat"))
        lon = (ChartAgent._find_col(schema, "longitude", "gps_lon", "gps_lng", "_lon", "_lng")
               or ChartAgent._find_col(schema, "lon", "lng"))
        return lat, lon

    # ISO-code column patterns: iso_code, stateISO, state_iso, iso2/iso3, cca2/cca3.
    # The Nigeria country_map matches on NG-XX ISO_3166-2 codes (not state names), so
    # an ISO column renders correctly while a names column ("Lagos State") goes blank.
    _ISO_COL_RE = re.compile(r"(?:^|_)iso|iso(?:[_0-9]|$)|cca[23]", re.I)

    @staticmethod
    def _region_col(schema: DatasetSchema, preferred: str | None = None) -> str | None:
        """A state/region column for the choropleth — STRONGLY prefer an ISO-coded
        column, since the country_map matches on ISO codes, not names."""
        for name in schema.columns:
            if ChartAgent._ISO_COL_RE.search(name):
                return name
        if preferred and preferred in schema.columns:
            return preferred
        return ChartAgent._find_col(schema, "state", "region", "province", "country") or preferred

    @staticmethod
    def _time_col(schema: DatasetSchema, preferred: str | None = None) -> str | None:
        """A temporal column for calendar/time-series viz types. Prefer `preferred`
        only when it's actually temporal; otherwise the first temporal column; else
        fall back to `preferred` (or None) so callers can detect 'no time column'."""
        if preferred and ChartAgent._is_temporal(schema, preferred):
            return preferred
        for name in schema.columns:
            if ChartAgent._is_temporal(schema, name):
                return name
        return preferred if (preferred and preferred in schema.columns) else None

    @staticmethod
    def _is_temporal(schema: DatasetSchema, col: str) -> bool:
        t = (schema.columns.get(col, "") or "").upper()
        return any(x in t for x in ("TIMESTAMP", "DATETIME", "DATE", "TIME"))

    @classmethod
    def _axis_col(cls, col: str, schema: DatasetSchema, grain: str | None):
        """For a REST query: return a bucketed adhoc BASE_AXIS column when `col` is a
        temporal column (so the time axis groups by the grain), else the plain name."""
        if grain and cls._is_temporal(schema, col):
            return {"timeGrain": grain, "columnType": "BASE_AXIS", "sqlExpression": col,
                    "label": col, "expressionType": "SQL"}
        return col

    @staticmethod
    def _iso_date(v) -> str | None:
        """Normalise a min/max probe value (epoch-ms number or date string) → 'YYYY-MM-DD'."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            try:
                return datetime.datetime.utcfromtimestamp(v / 1000).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                return None
        s = str(v).strip()
        return s[:10] if s else None

    @staticmethod
    def _extract_time_range(filters, tcol):
        """Pull a Superset 'since : until' time_range out of plan filters on the time
        column. Returns (range_str | None, remaining_filters). Only forms a range when
        BOTH bounds are present, so the time-axis viz gets explicit Since AND Until."""
        if not filters or not tcol:
            return None, filters
        since = until = None
        rest = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            col = f.get("col") or f.get("column") or f.get("subject") or f.get("field")
            op = str(f.get("op") or f.get("operator") or "").strip()
            val = f.get("val") if f.get("val") is not None else f.get("value")
            if col == tcol and op in (">=", ">"):
                since = val
            elif col == tcol and op in ("<=", "<"):
                until = val
            else:
                rest.append(f)
        if since is not None and until is not None:
            return f"{since} : {until}", rest
        return None, filters

    def _data_time_range(self, schema: DatasetSchema, tcol: str) -> str | None:
        """Probe the dataset for MIN/MAX of the time column → a bounded 'since : until'
        time_range, so a cal_heatmap spans the actual data instead of erroring on
        unbounded time. Returns None if the probe is unavailable/fails."""
        if not self.auth or not tcol:
            return None
        mn = {"expressionType": "SIMPLE", "column": {"column_name": tcol}, "aggregate": "MIN", "label": "mn"}
        mx = {"expressionType": "SIMPLE", "column": {"column_name": tcol}, "aggregate": "MAX", "label": "mx"}
        rows = self.auth.query_data(schema.id, {"metrics": [mn, mx], "columns": [], "row_limit": 1, "orderby": []})
        if not rows:
            return None
        since = self._iso_date(rows[0].get("mn"))
        until = self._iso_date(rows[0].get("mx"))
        if since and until:
            # make the upper bound inclusive of the last day
            try:
                until = (datetime.datetime.strptime(until, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            except ValueError:
                pass
            return f"{since} : {until}"
        return None

    def _build_rest_chart(self, spec: ChartSpec, schema: DatasetSchema):
        """Build (viz_type, form_data, query_context) for a REST-only chart.
        query_context is stored so the chart renders deterministically."""
        viz = _REST_VIZ[spec.chart_type.lower()]
        agg = (spec.aggregate or "COUNT").upper()
        col = spec.metric_column or next(iter(schema.columns), "")
        if agg == "COUNT" and (not col or col == "*"):
            col = spec.dimension or next(iter(schema.columns), "")
        label = spec.metric if (isinstance(spec.metric, str) and spec.metric) else f"{agg}({col})"
        metric = {"expressionType": "SIMPLE", "column": {"column_name": col},
                  "aggregate": agg, "label": label}
        # primary + extra metrics (radar/bubble can use several)
        metrics = [metric]
        for m in (spec.extra_metrics or []):
            if not isinstance(m, dict):
                continue
            c2 = m.get("metric_column") or m.get("name") or m.get("column")
            if not c2:
                continue
            a2 = (m.get("aggregate") or agg).upper()
            metrics.append({"expressionType": "SIMPLE", "column": {"column_name": c2},
                            "aggregate": a2, "label": m.get("label") or f"{a2}({c2})"})
        dims = [d for d in (spec.dimension, spec.series_column) if d] or \
               [next(iter(schema.columns), "")]
        primary_dim = spec.dimension or dims[0]
        row_limit = spec.row_limit if (spec.row_limit and spec.row_limit > 0) else 100
        ds = f"{schema.id}__table"
        # Filters to apply at the end; a viz branch may consume some (e.g. cal_heatmap
        # turns temporal-range filters into a bounded time_range instead).
        plan_filters = spec.filters

        if viz == "box_plot":
            form_data = {"viz_type": viz, "datasource": ds, "metrics": [metric],
                         "groupby": [primary_dim], "whisker_options": "Tukey", "row_limit": 1000}
            query = {"metrics": [metric], "columns": [primary_dim], "row_limit": 1000, "orderby": [],
                     "post_processing": [{"operation": "boxplot", "options": {
                         "whisker_type": "tukey", "groupby": [primary_dim], "metrics": [label]}}]}
        elif viz == "sunburst_v2":
            form_data = {"viz_type": viz, "datasource": ds, "columns": dims, "metric": metric, "row_limit": row_limit}
            query = {"metrics": [metric], "columns": dims, "row_limit": row_limit, "orderby": [[label, False]]}
        elif viz == "waterfall":
            form_data = {"viz_type": viz, "datasource": ds, "metric": metric, "x_axis": primary_dim, "row_limit": row_limit}
            query = {"metrics": [metric], "columns": [primary_dim], "row_limit": row_limit, "orderby": [[label, False]]}
        elif viz == "gauge_chart":
            gb = [primary_dim] if spec.dimension else []
            form_data = {"viz_type": viz, "datasource": ds, "metric": metric, "groupby": gb, "row_limit": 10}
            query = {"metrics": [metric], "columns": gb, "row_limit": 10, "orderby": [[label, False]]}
        elif viz == "radar":
            form_data = {"viz_type": viz, "datasource": ds, "metrics": metrics, "groupby": [primary_dim], "row_limit": row_limit}
            query = {"metrics": metrics, "columns": [primary_dim], "row_limit": row_limit, "orderby": []}
        elif viz == "sankey_v2":
            source = primary_dim
            target = spec.series_column or (dims[1] if len(dims) > 1 else primary_dim)
            form_data = {"viz_type": viz, "datasource": ds, "source": source, "target": target, "metric": metric}
            query = {"metrics": [metric], "columns": [source, target], "row_limit": 200, "orderby": [[label, False]]}
        elif viz == "histogram_v2":
            # histogram_v2 bins a raw numeric column (no aggregate); optional groupby.
            hist_col = spec.metric_column if (spec.metric_column and spec.metric_column != "*") else col
            grp = [spec.dimension] if (spec.dimension and spec.dimension != hist_col) else []
            form_data = {"viz_type": viz, "datasource": ds, "column": hist_col, "groupby": grp,
                         "bins": "10", "normalize": False, "cumulative": False, "row_limit": 50000,
                         "x_axis_format": ",d", "y_axis_format": "SMART_NUMBER"}
            query = {"columns": [hist_col] + grp, "row_limit": 50000, "orderby": []}
        elif viz == "bubble_v2":
            count_m = {"expressionType": "SIMPLE", "column": {"column_name": col},
                       "aggregate": "COUNT", "label": f"COUNT({col})"}
            x_m = metrics[0]
            y_m = metrics[1] if len(metrics) > 1 else count_m
            size_m = metrics[2] if len(metrics) > 2 else metrics[0]
            qm, seen = [], set()
            for mm in (x_m, y_m, size_m):
                if mm["label"] not in seen:
                    qm.append(mm); seen.add(mm["label"])
            form_data = {"viz_type": viz, "datasource": ds, "x": x_m, "y": y_m, "size": size_m,
                         "entity": primary_dim, "row_limit": row_limit}
            query = {"metrics": qm, "columns": [primary_dim], "row_limit": row_limit, "orderby": []}
        elif viz == "heatmap_v2":
            # Real eCharts heatmap: x_axis × groupby (a SINGLE category) matrix. When
            # the x_axis is a temporal column, bucket it by the grain (e.g. errors by
            # month × type) so it isn't one column per raw timestamp.
            x_ax = primary_dim
            grp = spec.series_column or (dims[1] if len(dims) > 1 else primary_dim)
            grain = spec.time_grain or ("P1D" if self._is_temporal(schema, x_ax) else None)
            xcol = self._axis_col(x_ax, schema, grain)
            form_data = {"viz_type": viz, "datasource": ds, "x_axis": xcol, "groupby": grp,
                         "metric": metric, "row_limit": 50000, "normalize_across": "heatmap",
                         "sort_x_axis": "alpha_asc", "sort_y_axis": "alpha_asc",
                         "legend_type": "continuous", "linear_color_scheme": "superset_seq_1",
                         "y_axis_format": "SMART_NUMBER"}
            query = {"metrics": [metric], "columns": [xcol, grp], "row_limit": 50000, "orderby": []}
        elif viz == "country_map":
            # Choropleth — value shaded per state/region; prefer an ISO-coded column.
            entity = self._region_col(schema, primary_dim) or primary_dim
            form_data = {"viz_type": viz, "datasource": ds, "entity": entity,
                         "select_country": "nigeria", "metric": metric,
                         "linear_color_scheme": "schemeMagma", "number_format": "SMART_NUMBER"}
            query = {"metrics": [metric], "columns": [entity], "row_limit": 1000,
                     "orderby": [[label, False]]}
        elif viz == "cal_heatmap":
            # Calendar heatmap — a metric per day across a month/year grid. The viz
            # REQUIRES bounded time (Since AND Until) or it errors "Please provide both
            # time bounds". Use the plan's date-range filters if present, else the
            # dataset's actual min/max so the calendar spans the real data.
            tcol = self._time_col(schema, spec.time_column or primary_dim)
            if not tcol or not self._is_temporal(schema, tcol):
                raise ValueError("cal_heatmap requires a temporal column")
            time_range, plan_filters = self._extract_time_range(spec.filters, tcol)
            if not time_range:
                time_range = self._data_time_range(schema, tcol)
            if not time_range:
                # cal_heatmap errors without Since AND Until — never leave it unbounded;
                # drop the chart so the pipeline can fall back instead of rendering broken.
                raise ValueError("cal_heatmap needs a bounded time_range (no filters / probe failed)")
            form_data = {"viz_type": viz, "datasource": ds, "granularity_sqla": tcol,
                         "time_range": time_range, "domain_granularity": "month",
                         "subdomain_granularity": "day", "metrics": [metric],
                         "cell_size": 10, "cell_padding": 2, "steps": 10,
                         "linear_color_scheme": "superset_seq_1"}
            query = {"metrics": [metric], "columns": [], "granularity": tcol,
                     "time_range": time_range, "row_limit": 1000, "orderby": []}
            log.info("cal_heatmap '%s': time_range=%s", spec.name, time_range)
        elif viz == "echarts_timeseries_smooth":
            # Smooth (curved) line time series. Bucket by grain only when the x-axis is
            # actually temporal; otherwise plot the line over the plain column.
            tcol = self._time_col(schema, spec.time_column or primary_dim) \
                or primary_dim or next(iter(schema.columns), "")
            grain = spec.time_grain or "P1D"
            xcol = self._axis_col(tcol, schema, grain)
            grp = [spec.series_column] if (spec.series_column and spec.series_column != tcol) else []
            form_data = {"viz_type": viz, "datasource": ds, "x_axis": xcol, "metrics": metrics,
                         "groupby": grp, "row_limit": row_limit, "show_legend": True}
            if self._is_temporal(schema, tcol):
                form_data["time_grain_sqla"] = grain
            query = {"metrics": metrics, "columns": [xcol] + grp,
                     "series_columns": grp, "row_limit": row_limit, "orderby": []}
        elif viz == "deck_screengrid":
            # Geospatial density — needs latitude/longitude columns + a Mapbox token.
            lat, lon = self._geo_cols(schema)
            if not (lat and lon):
                raise ValueError("deck_screengrid requires latitude/longitude columns")
            size_m = {"expressionType": "SIMPLE", "column": {"column_name": lat},
                      "aggregate": "COUNT", "label": f"COUNT({lat})"}
            form_data = {"viz_type": viz, "datasource": ds,
                         "spatial": {"type": "latlong", "latCol": lat, "lonCol": lon},
                         "size": size_m, "row_limit": 10000, "grid_size": 20,
                         "mapbox_style": "mapbox://styles/mapbox/light-v9", "autozoom": True,
                         "viewport": {"bearing": 0, "latitude": 9.08, "longitude": 8.68,
                                      "pitch": 0, "zoom": 5},
                         "color_picker": {"a": 1, "r": 0, "g": 122, "b": 135}, "js_columns": []}
            query = {"metrics": [size_m], "columns": [lat, lon], "row_limit": 10000, "orderby": [],
                     "filters": [{"col": lat, "op": "IS NOT NULL"},
                                 {"col": lon, "op": "IS NOT NULL"}]}
        else:  # treemap_v2, funnel
            form_data = {"viz_type": viz, "datasource": ds, "metric": metric, "groupby": dims, "row_limit": row_limit}
            query = {"metrics": [metric], "columns": dims, "row_limit": row_limit, "orderby": [[label, False]]}

        # Propagate plan filters (date ranges, status='COMPLETED', amount>1000, …).
        # The MCP path applies these via config; the REST path must add them to BOTH
        # the stored query_context (which drives rendering) and form_data.adhoc_filters
        # (so they also show in the Explore UI). Without this, a chart titled e.g.
        # "Jan–Mar 2026" would silently render all-time data.
        rest_filters = self._normalise_filters(plan_filters)
        if rest_filters:
            # Superset's query_context + adhoc filters use "==" for equality, while
            # the MCP format (_normalise_filters) uses "=". Map it for both.
            conv = [{**f, "op": ("==" if f["op"] == "=" else f["op"])} for f in rest_filters]
            query["filters"] = (query.get("filters") or []) + conv
            adhoc = [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": f["col"],
                      "operator": f["op"], "comparator": f["val"]} for f in conv]
            form_data["adhoc_filters"] = (form_data.get("adhoc_filters") or []) + adhoc
            log.info("REST chart '%s': applied %d filter(s): %s", spec.name, len(conv), conv)

        query_context = {"datasource": {"id": schema.id, "type": "table"}, "force": False,
                         "result_format": "json", "result_type": "full",
                         "form_data": form_data, "queries": [query]}
        log.info("REST chart '%s' → viz=%s, dims=%s, metric=%s", spec.name, viz, dims, label)
        return viz, form_data, query_context

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
            "big_number":   self._big_number_config,
            "mixed":        self._mixed_timeseries_config,  # combo: dual-axis time series
            # NOTE: box_plot/funnel/radar/waterfall/treemap/sunburst are NOT
            # supported by the MCP — _FAMILY_MAP remaps them to xy/pie above, so
            # their legacy builders below are unreachable.
            "waterfall":    self._waterfall_config,
            "treemap":      self._treemap_config,
            "sunburst":     self._sunburst_config,
            "box_plot":     self._box_plot_config,
            "funnel":       self._funnel_config,
            "radar":        self._radar_config,
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
            grain = spec.time_grain or "P1D"
            config["time_grain"] = grain
            log.info("XY chart '%s': time-series x=%s grain=%s", spec.name, spec.time_column, grain)

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

    # ── Combo (mixed_timeseries) ──────────────────────────────────────

    def _mixed_timeseries_config(self, spec: ChartSpec, schema: DatasetSchema) -> dict:
        """
        Dual-axis time series: the primary metric as bars and the first extra
        metric as a line on a secondary axis — e.g. "transaction count and
        average amount over time". Falls back to bars-only if there's no second
        metric.
        """
        x_col = spec.time_column or spec.dimension or ""
        col_type = schema.columns.get(x_col, "")
        config: dict = {
            "chart_type": "mixed_timeseries",
            "x": {"name": x_col, "dtype": col_type},
            "y": [{
                "name": spec.metric_column,
                "aggregate": spec.aggregate.upper(),
                "label": spec.metric if isinstance(spec.metric, str) else spec.metric_column,
            }],
            "primary_kind": "bar",
            "secondary_kind": "line",
        }
        if any(t in col_type.upper() for t in ("TIMESTAMP", "DATETIME", "DATE", "TIME")):
            config["time_grain"] = spec.time_grain or "P1D"

        extra = spec.extra_metrics or []
        if extra and isinstance(extra[0], dict):
            m = extra[0]
            col = m.get("metric_column") or m.get("name") or m.get("column")
            if col:
                agg = (m.get("aggregate") or spec.aggregate).upper()
                config["y_secondary"] = [{"name": col, "aggregate": agg,
                                          "label": m.get("label") or col}]
        log.info("Mixed/combo chart '%s': x=%s, secondary=%s",
                 spec.name, x_col, bool(config.get("y_secondary")))
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