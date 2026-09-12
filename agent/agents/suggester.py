"""
Suggester — turns the live dataset catalog/profile into useful prompts:
  - starter_suggestions: dataset-grounded example queries for the welcome screen
  - followup_suggestions: next questions that extend the dashboard in view
Both are LLM-generated with a strict JSON schema and grounded in real columns;
starters are cached. They are small calls, so they run at LLM_REASONING_EFFORT_FAST.
"""

import hashlib
import logging

import cache
from config import LLM_REASONING_EFFORT_FAST
from llm_client import LLMError

log = logging.getLogger(__name__)

STARTER_TTL = 1800  # 30 min

# Shown if the LLM/catalog is unavailable, so chips never render empty.
FALLBACK_STARTERS = [
    "show me a time series chart of inflow and outflow of cash",
    "show the top 10 banks by total transaction amount as a bar chart grouped by bank_name",
    "Create a pie chart of number of transactions done by each channel",
]

_SUGGESTIONS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["suggestions"],
    "properties": {"suggestions": {"type": "array", "items": {"type": "string"}}},
}

_STARTER_SYSTEM = """Suggest example questions for a chat assistant that turns plain-English questions into Superset dashboards. Each must be answerable from ONE of the given datasets using its real columns. Cover different kinds of analysis: a trend over time, a top-N ranking, a share or breakdown, two measures compared over time, a distribution, and one or two open questions ("give me an overview of ...") that would produce a small multi-chart dashboard. Include a geographic breakdown only if a dataset has a state or region column. Keep each under 14 words, in plain business language."""

_FOLLOWUP_SYSTEM = """Suggest the next questions a user could ask to extend the dashboard they are viewing. Each should add something the dashboard doesn't show yet: another dimension or measure, a trend or a different time grain, a distribution, a headline number, or a pair of related views in one question ("add a monthly trend and a breakdown by state"). Stay within what the dataset supports and don't repeat existing charts. NULLABLE columns can only be counted, not summed or averaged. Keep each under 14 words."""


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
        data = llm.generate_json(_STARTER_SYSTEM, user, schema=_SUGGESTIONS_SCHEMA,
                                 schema_name="suggestions",
                                 reasoning_effort=LLM_REASONING_EFFORT_FAST or None)
    except LLMError as e:
        log.warning("starter suggestions failed: %s", e)
        return []
    return _clean(data, limit=6)


def followup_suggestions(query, dataset_name, profile_text, existing_charts, llm,
                         earlier_queries=None) -> list:
    """3 next questions for the dashboard in view. `existing_charts` should cover
    the whole dashboard, and `earlier_queries` the questions asked before `query`."""
    asked = [q for q in (earlier_queries or []) if q] + [query]
    user = (
        f'Dataset: "{dataset_name}"\n'
        f"Profile:\n{profile_text or '(none)'}\n"
        f"Charts on the dashboard: {existing_charts or '(none)'}\n"
        f"Questions asked so far: {asked}\n\nSuggest 3 follow-up questions."
    )
    try:
        data = llm.generate_json(_FOLLOWUP_SYSTEM, user, schema=_SUGGESTIONS_SCHEMA,
                                 schema_name="suggestions",
                                 reasoning_effort=LLM_REASONING_EFFORT_FAST or None)
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
