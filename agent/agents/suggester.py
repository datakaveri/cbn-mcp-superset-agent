"""
Suggester — turns the live dataset catalog/profile into useful prompts:
  - starter_suggestions: dataset-grounded example queries for the welcome screen
  - followup_suggestions: contextual next questions after a dashboard is built
Both are LLM-generated (JSON mode) and grounded in real columns; starters are cached.
"""

import hashlib
import logging

import cache
from llm_client import LLMError

log = logging.getLogger(__name__)

STARTER_TTL = 1800  # 30 min

# Shown if the LLM/catalog is unavailable, so chips never render empty.
FALLBACK_STARTERS = [
    "show me a time series chart of inflow and outflow of cash",
    "show the top 10 banks by total transaction amount as a bar chart grouped by bank_name",
    "Create a pie chart of number of transactions done by each channel",
]

_STARTER_SYSTEM = """You suggest example analytics questions for a chart/dashboard agent.
Given real datasets and their columns, produce SHORT, specific, runnable
natural-language questions a business user would ask — each mapping to real
columns of ONE dataset. VARY the analysis type across the set, since the agent can
build many chart kinds: a trend over time, a top-N ranking, a breakdown/share, a
comparison of two measures over time, a distribution/spread of a numeric column, a
flow between two categories, a hierarchy/part-of-whole, and a single KPI. Keep each
under ~14 words, no IDs/jargon.
Respond ONLY with JSON: {"suggestions": ["...", "..."]}"""

_FOLLOWUP_SYSTEM = """You suggest follow-up questions to ADD complementary charts to an
existing dashboard. Given the dataset profile and the charts already on it,
propose SHORT next questions that add a DIFFERENT view — vary the angle AND the
chart kind (another dimension or measure, a time trend, a distribution/spread, a
flow, a hierarchy/part-of-whole, or a single KPI) — do not duplicate existing
charts. Prefer low-cardinality columns as dimensions and never aggregate NULLABLE
columns. Respond ONLY with JSON: {"suggestions": ["...", "..."]}"""


def starter_suggestions(catalog, dataset_agent, llm, n_ground: int = 6) -> list:
    """Dataset-grounded starter queries (cached by catalog signature)."""
    sig = hashlib.md5("|".join(sorted(c.table_name for c in catalog)).encode()).hexdigest()[:12]
    result = cache.get_or_compute(
        f"starters:{sig}", STARTER_TTL,
        lambda: _starters(catalog, dataset_agent, llm, n_ground),
    )
    return result or FALLBACK_STARTERS


def _starters(catalog, dataset_agent, llm, n_ground) -> list:
    grounding = []
    for s in catalog[:n_ground]:
        enriched = dataset_agent.enrich(s)
        if enriched.columns:
            cols = ", ".join(list(enriched.columns.keys())[:18])
            grounding.append(f'- "{enriched.table_name}": {cols}')
    if not grounding:
        grounding = [f'- "{s.table_name}"' for s in catalog[:n_ground]]

    user = "DATASETS (use ONLY these columns):\n" + "\n".join(grounding) + \
           "\n\nProduce 6 starter questions."
    try:
        data = llm.generate_json(_STARTER_SYSTEM, user)
    except LLMError as e:
        log.warning("starter suggestions failed: %s", e)
        return []
    return _clean(data, limit=6)


def followup_suggestions(query, dataset_name, profile_text, existing_charts, llm) -> list:
    """3 contextual next questions for the dashboard just built."""
    user = (
        f'Dataset: "{dataset_name}"\n'
        f"Profile:\n{profile_text}\n"
        f"Charts already on the dashboard: {existing_charts or '(none)'}\n"
        f"User's last request: {query}\n\nSuggest 3 follow-up questions."
    )
    try:
        data = llm.generate_json(_FOLLOWUP_SYSTEM, user)
    except LLMError as e:
        log.info("followup suggestions failed: %s", e)
        return []
    return _clean(data, limit=3)


def _clean(data, limit) -> list:
    items = data.get("suggestions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for s in items:
        s = str(s).strip()
        if s:
            out.append(s)
    return out[:limit]
