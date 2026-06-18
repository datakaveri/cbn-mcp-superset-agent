"""
Data models for the Superset MCP Agentic Pipeline.
Every agent returns an AgentResult for consistent error handling.
"""
 
from __future__ import annotations
 
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
 
 
class Phase(Enum):
    """Pipeline execution phases."""
    HEALTH_CHECK = "health_check"
    PLAN_GENERATION = "plan_generation"
    DATASET_DISCOVERY = "dataset_discovery"
    PLAN_REFINEMENT = "plan_refinement"
    SQL_VALIDATION = "sql_validation"
    CHART_CREATION = "chart_creation"
    DASHBOARD_ASSEMBLY = "dashboard_assembly"
    RESULT_REPORTING = "result_reporting"
 
 
@dataclass
class AgentResult:
    """
    Universal return type for every agent.
    The self-correction loop inspects these to decide: retry, skip, or abort.
    """
    success: bool
    data: Any = None
    error: Optional[str] = None
    details: Optional[dict] = None
 
    @staticmethod
    def ok(data: Any, details: Optional[dict] = None) -> AgentResult:
        return AgentResult(success=True, data=data, details=details)
 
    @staticmethod
    def fail(error: str, details: Optional[dict] = None) -> AgentResult:
        return AgentResult(success=False, error=error, details=details)
 
 
@dataclass
class ChartSpec:
    """A single chart to be created, as planned by the Orchestrator."""
    name: str
    chart_type: str          # e.g. "bar", "line", "pie", "table", "box_plot"
    metric: str              # e.g. "SUM(amount)" or "COUNT(tx_id)"
    metric_column: str       # the raw column name for the metric
    aggregate: str           # e.g. "SUM", "COUNT", "AVG"
    dimension: str           # e.g. "state", "bank_name"
    time_column: Optional[str] = None    # for temporal charts
    filters: Optional[list] = None       # list of {col, op, val} dicts
    # Chart-level display options — populated from LLM plan extras
    stack: bool = False                  # stacked bar/area
    row_limit: Optional[int] = None      # top-N rows (applied via SQL ORDER BY/LIMIT)
    # For multi-metric charts (e.g. grouped bar with DEPOSIT + WITHDRAWAL)
    extra_metrics: Optional[list] = None  # list of {metric_column, aggregate, label}
    # For charts that group series by a column (e.g. type for stacked bar)
    series_column: Optional[str] = None   # column used to split series
 
 
@dataclass
class PipelinePlan:
    """The full plan produced by the Orchestrator from a user query."""
    datasets: list[str]
    charts: list[ChartSpec]
    dashboard_title: str
 
 
@dataclass
class DatasetSchema:
    """Schema info for a discovered dataset."""
    id: int
    name: str
    database_id: int
    columns: dict[str, str] = field(default_factory=dict)  # {col_name: col_type}
    table_name: str = ""
    schema_name: str = ""   # DB schema/namespace (e.g. ClickHouse "cbn_simulation_data")
 
 
@dataclass
class ChartResult:
    """Result of creating a single chart."""
    spec: ChartSpec
    chart_id: Optional[int] = None
    success: bool = False
    error: Optional[str] = None
    retries: int = 0
 
 
@dataclass
class PipelineReport:
    """Final output of the pipeline — shown to the user."""
    dashboard_url: Optional[str] = None
    dashboard_id: Optional[int] = None
    charts_created: list[ChartResult] = field(default_factory=list)
    sql_previews: dict[str, list] = field(default_factory=dict)  # {chart_name: rows}
    errors: list[str] = field(default_factory=list)
    success: bool = False
 
 
@dataclass
class LogEntry:
    """A single log line for the TUI."""
    phase: Phase
    level: str   # "info", "success", "warning", "error"
    message: str
    timestamp: str = ""