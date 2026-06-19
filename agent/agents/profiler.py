"""
Dataset Profiler — gives the planner a real understanding of a dataset.

Runs a few cheap execute_sql probes (cached) to learn, per column:
  - role: time / measure / dimension (from type + cardinality)
  - cardinality (distinct count) → good dimensions are low-cardinality
  - sample distinct values for categoricals → semantic hints (type ∈ {DEPOSIT,…})
  - numeric min/max/avg → identifies real measures
This profile is fed into the planning prompt so the LLM picks sensible
dimensions, metrics and chart types instead of a generic COUNT(*).
"""

import logging

import cache
from mcp_client import MCPClient
from models import ColumnProfile, DatasetProfile, DatasetSchema

log = logging.getLogger(__name__)

PROFILE_TTL = 1800        # 30 min — datasets rarely change shape
MAX_CARD_COLS = 16        # cap columns we cardinality-probe in one pass
LOW_CARD = 50             # ≤ this distinct → a categorical worth sampling
SAMPLE_COLS = 6           # cap how many low-card columns we sample distinct values for
SAMPLE_LIMIT = 12

_NUMERIC_HINTS = ("INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL")
_TEMPORAL_HINTS = ("TIMESTAMP", "DATETIME", "DATE", "TIME")


def _is_numeric(t: str) -> bool:
    t = t.upper()
    return any(h in t for h in _NUMERIC_HINTS)


def _is_temporal(t: str) -> bool:
    return any(h in t.upper() for h in _TEMPORAL_HINTS)


def _is_nullable(t: str) -> bool:
    return "nullable" in t.lower()


def _from_source(schema: DatasetSchema) -> str:
    """FROM target: wrap a virtual dataset's SQL, else qualify the physical table."""
    if schema.sql:
        return f"({schema.sql.rstrip(';').strip()}) AS t"
    tbl = schema.table_name or schema.name
    if schema.schema_name and "." not in tbl:
        tbl = f"{schema.schema_name}.{tbl}"
    return tbl


def profile_dataset(schema: DatasetSchema, mcp: MCPClient) -> DatasetProfile:
    """Profile a dataset (cached by id). schema must already be enriched (columns)."""
    return cache.get_or_compute(
        f"profile:{schema.id}", PROFILE_TTL, lambda: _profile(schema, mcp)
    )


def _profile(schema: DatasetSchema, mcp: MCPClient) -> DatasetProfile:
    src = _from_source(schema)
    sch = schema.schema_name or "public"
    cols = list(schema.columns.items())

    profile = DatasetProfile(dataset_id=schema.id)
    by_name: dict[str, ColumnProfile] = {}
    for name, ctype in cols:
        cp = ColumnProfile(
            name=name, type=ctype,
            is_numeric=_is_numeric(ctype), is_temporal=_is_temporal(ctype),
            is_nullable=_is_nullable(ctype),
        )
        cp.role = "time" if cp.is_temporal else ("measure" if cp.is_numeric else "dimension")
        profile.columns.append(cp)
        by_name[name] = cp

    def run(sql: str):
        r = mcp.execute_sql(schema.database_id, sql, schema=sch)
        if not r.success:
            log.info("Profiler probe skipped (%s): %s", schema.name, (r.error or "")[:120])
            return None
        rows = _rows(r.data)
        return rows[0] if rows else None

    # ── 1) row_count + per-column cardinality (one pass) ──
    card_cols = [n for n, _ in cols][:MAX_CARD_COLS]
    selects = ["count(*) AS __n"] + [f"uniq({n}) AS k{i}" for i, n in enumerate(card_cols)]
    row = run(f"SELECT {', '.join(selects)} FROM {src}")
    if row:
        profile.row_count = _as_int(row.get("__n"))
        for i, n in enumerate(card_cols):
            by_name[n].cardinality = _as_int(row.get(f"k{i}"))

    # ── 2) numeric min/max/avg (one pass over numeric columns) ──
    num_cols = [c.name for c in profile.columns if c.is_numeric]
    if num_cols:
        sel = []
        for i, n in enumerate(num_cols):
            sel += [f"min({n}) AS mn{i}", f"max({n}) AS mx{i}", f"round(avg({n}),2) AS av{i}"]
        row = run(f"SELECT {', '.join(sel)} FROM {src}")
        if row:
            for i, n in enumerate(num_cols):
                by_name[n].num_min = _as_float(row.get(f"mn{i}"))
                by_name[n].num_max = _as_float(row.get(f"mx{i}"))
                by_name[n].num_avg = _as_float(row.get(f"av{i}"))

    # ── 3) sample distinct values for low-cardinality dimensions ──
    sampled = 0
    for cp in profile.columns:
        if sampled >= SAMPLE_COLS:
            break
        if cp.role == "dimension" and cp.cardinality and cp.cardinality <= LOW_CARD:
            rows = _rows_of(mcp, schema, sch, f"SELECT DISTINCT {cp.name} FROM {src} LIMIT {SAMPLE_LIMIT}")
            if rows:
                cp.sample_values = [list(r.values())[0] for r in rows if r]
                sampled += 1

    log.info("Profiled '%s': %s rows, %d cols (%d sampled)",
             schema.name, profile.row_count, len(profile.columns), sampled)
    return profile


def _rows_of(mcp, schema, sch, sql):
    r = mcp.execute_sql(schema.database_id, sql, schema=sch)
    return _rows(r.data) if r.success else []


def _rows(data) -> list:
    """Normalise execute_sql's row payload (mirrors sql_agent._extract_rows)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rows", "result", "data"):
            cand = data.get(key)
            if isinstance(cand, list):
                return cand
            if isinstance(cand, dict):
                inner = cand.get("rows") or cand.get("data")
                if isinstance(inner, list):
                    return inner
    return []


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
