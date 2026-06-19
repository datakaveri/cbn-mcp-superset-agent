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
 
        # Verify the dashboard exists and capture its uuid (needed for the
        # embedded-SDK guest-token preview in the agent UI).
        dashboard_uuid = ""
        verify_result = self.mcp.get_dashboard_info(dashboard_id)
        if not verify_result.success:
            log.warning("Dashboard verification failed: %s", verify_result.error)
        else:
            vd = verify_result.data
            vres = vd.get("result", vd) if isinstance(vd, dict) else {}
            dashboard_uuid = (vres.get("uuid") or "") if isinstance(vres, dict) else ""
            log.info("Dashboard verified (id=%d, uuid=%s)", dashboard_id, dashboard_uuid or "?")
 
        url = f"{SUPERSET_BASE_URL}/superset/dashboard/{dashboard_id}/"

        return AgentResult.ok({
            "dashboard_id": dashboard_id,
            "uuid": dashboard_uuid,
            "url": url,
            "chart_count": len(chart_ids),
            "chart_ids": chart_ids,
        })

    def add_charts(self, dashboard_id, chart_results: list[ChartResult]) -> AgentResult:
        """
        Append successfully-created charts to an EXISTING dashboard (follow-up flow).
        Returns the same shape as create_dashboard (with the dashboard's embed uuid).
        """
        chart_ids = [r.chart_id for r in chart_results if r.success and r.chart_id]
        if not chart_ids:
            return AgentResult.fail("No charts to add — all chart creations failed")

        dashboard_id = int(dashboard_id)
        added = []
        for cid in chart_ids:
            res = self.mcp.add_chart_to_existing_dashboard(dashboard_id, cid)
            if res.success:
                added.append(cid)
            else:
                log.warning("Failed to add chart %s to dashboard %s: %s",
                            cid, dashboard_id, res.error)
        if not added:
            return AgentResult.fail("Could not add any charts to the dashboard")

        # Re-read the dashboard to capture its embed uuid for the inline preview.
        dashboard_uuid = ""
        verify = self.mcp.get_dashboard_info(dashboard_id)
        if verify.success:
            vd = verify.data
            vres = vd.get("result", vd) if isinstance(vd, dict) else {}
            dashboard_uuid = (vres.get("uuid") or "") if isinstance(vres, dict) else ""

        url = f"{SUPERSET_BASE_URL}/superset/dashboard/{dashboard_id}/"
        return AgentResult.ok({
            "dashboard_id": dashboard_id,
            "uuid": dashboard_uuid,
            "url": url,
            "chart_count": len(added),
            "chart_ids": added,
        })