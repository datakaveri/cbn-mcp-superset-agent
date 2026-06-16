"""
Dashboard Agent — Phase 5: Dashboard assembly.
Creates a dashboard from chart IDs via MCP and verifies it exists.
"""
 
import logging
 
from mcp_client import MCPClient
from models import AgentResult, ChartResult
from config import SUPERSET_BASE_URL
 
log = logging.getLogger(__name__)
 
 
class DashboardAgent:
    """Assembles and publishes Superset dashboards via MCP."""
 
    def __init__(self, mcp: MCPClient):
        self.mcp = mcp
 
    def create_dashboard(
        self,
        title: str,
        chart_results: list[ChartResult],
    ) -> AgentResult:
        """
        Create a dashboard from successfully created charts.
        Verifies the dashboard exists before returning the URL.
        """
        chart_ids = [r.chart_id for r in chart_results if r.success and r.chart_id]
 
        if not chart_ids:
            return AgentResult.fail("No charts to add — all chart creations failed")
 
        log.info("Creating dashboard '%s' with %d charts: %s", title, len(chart_ids), chart_ids)
 
        result = self.mcp.generate_dashboard({
            "dashboard_title": title,
            "chart_ids": chart_ids,
            "published": True,
        })
 
        if not result.success:
            return AgentResult.fail(f"Failed to create dashboard: {result.error}")
 
        data = result.data
        dashboard_id = None
 
        if isinstance(data, dict):
            dashboard_id = (
                data.get("id")
                or data.get("dashboard_id")
                or (data.get("result") or {}).get("id")
                or (data.get("dashboard") or {}).get("id")
            )
 
        if not dashboard_id:
            return AgentResult.fail(f"Dashboard created but no ID returned: {data}")
 
        dashboard_id = int(dashboard_id)
 
        # Verify the dashboard exists
        verify_result = self.mcp.get_dashboard_info(dashboard_id)
        if not verify_result.success:
            log.warning("Dashboard verification failed: %s", verify_result.error)
        else:
            log.info("Dashboard verified (id=%d)", dashboard_id)
 
        url = f"{SUPERSET_BASE_URL}/superset/dashboard/{dashboard_id}/"
 
        return AgentResult.ok({
            "dashboard_id": dashboard_id,
            "url": url,
            "chart_count": len(chart_ids),
            "chart_ids": chart_ids,
        })