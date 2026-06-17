"""
MCP client for the Superset MCP service at localhost:5008/mcp.
Uses JSON-RPC over streamable-HTTP transport.
All tool arguments are wrapped in {"request": {...}} as required by the Superset MCP server.
"""
 
import json
import logging
import requests
from typing import Any, Optional
 
from config import MCP_URL, MCP_AUTH_TOKEN, REQUEST_TIMEOUT
from models import AgentResult
 
log = logging.getLogger(__name__)
 
 
class MCPClient:
    """Stateful MCP client — initializes a session, then calls tools."""
 
    def __init__(self, base_url: str = MCP_URL, auth_token: str = MCP_AUTH_TOKEN):
        self.base_url = base_url
        self.auth_token = auth_token
        self.session_id: Optional[str] = None
        self._request_id = 0
        self._http = requests.Session()
        self._http.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        # The hosted MCP endpoint requires a Bearer token on every request.
        if auth_token:
            self._http.headers["Authorization"] = f"Bearer {auth_token}"
 
    # ── Low-level transport ──────────────────────────────────────────
 
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id
 
    def _send(self, method: str, params: Optional[dict] = None,
              is_notification: bool = False) -> Optional[dict]:
        """Send a JSON-RPC request/notification to the MCP endpoint."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        if not is_notification:
            payload["id"] = self._next_id()
 
        headers = dict(self._http.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
 
        try:
            resp = self._http.post(
                self.base_url, json=payload,
                headers=headers, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise MCPError(f"MCP request failed: {e}") from e
 
        if "Mcp-Session-Id" in resp.headers:
            self.session_id = resp.headers["Mcp-Session-Id"]
 
        if is_notification:
            return None
 
        content_type = resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text)
        return resp.json()
 
    def _parse_sse(self, body: str) -> dict:
        """
        Extract the JSON-RPC result from an SSE stream.
        The server sends notification events (no 'id') then the actual RPC
        response (has 'id', no 'method'). We prefer the RPC response event.
        """
        result_event = None
        last_data = None
 
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                parsed = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            last_data = parsed
            if "id" in parsed and "method" not in parsed:
                result_event = parsed
 
        chosen = result_event or last_data
        if chosen is None:
            raise MCPError("No valid data found in SSE stream")
        return chosen
 
    # ── Session lifecycle ────────────────────────────────────────────
 
    def initialize(self) -> dict:
        """Initialize the MCP session. Must be called before any tool use."""
        result = self._send("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "superset-agent", "version": "1.0.0"},
        })
        self._send("notifications/initialized", is_notification=True)
        log.info("MCP session initialized (session_id=%s)", self.session_id)
        return result
 
    def list_tools(self) -> list[dict]:
        """List all available MCP tools."""
        result = self._send("tools/list")
        return result.get("result", {}).get("tools", [])
 
    # ── Tool calling ─────────────────────────────────────────────────
 
    def call_tool(self, tool_name: str, arguments: dict = None) -> AgentResult:
        """
        Call an MCP tool and return an AgentResult.
        NOTE: arguments should already be in the correct shape for the tool
        (i.e. {"request": {...}} for Superset MCP tools, or flat for meta-tools).
        """
        arguments = arguments or {}
        log.info("MCP call: %s(%s)", tool_name, json.dumps(arguments, default=str)[:200])
 
        try:
            response = self._send("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
        except MCPError as e:
            return AgentResult.fail(str(e))
 
        if "error" in response:
            err = response["error"]
            msg = err.get("message", str(err))
            return AgentResult.fail(f"MCP error: {msg}", details=err)
 
        result = response.get("result", {})
        content = result.get("content", [])
        is_error = result.get("isError", False)
 
        text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
        combined_text = "\n".join(text_parts)
 
        parsed = combined_text
        try:
            parsed = json.loads(combined_text)
        except (json.JSONDecodeError, TypeError):
            pass
 
        if is_error:
            return AgentResult.fail(
                error=combined_text if isinstance(parsed, str) else str(parsed),
                details=parsed if isinstance(parsed, dict) else None,
            )
 
        # Many Superset MCP tools (execute_sql, generate_chart, …) signal a
        # tool-level failure *inside* a 200 response via "success": false plus an
        # "error" field. Surface that as a real failure instead of letting callers
        # mistake it for empty/ID-less success.
        if isinstance(parsed, dict) and parsed.get("success") is False:
            err = parsed.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("details") or str(err)
            else:
                msg = str(err) if err else "MCP tool reported success=false"
            import html
            return AgentResult.fail(html.unescape(msg), details=parsed)

        return AgentResult.ok(parsed)
 
    # ── Convenience wrappers ─────────────────────────────────────────
    # All Superset MCP tools use {"request": {...}} wrapping.
 
    def health_check(self) -> AgentResult:
        return self.call_tool("health_check")
 
    def list_datasets(self, page_size: int = 100) -> AgentResult:
        return self.call_tool("list_datasets", {
            "request": {"page_size": page_size}
        })
 
    def get_dataset_info(self, dataset_id: int) -> AgentResult:
        return self.call_tool("get_dataset_info", {
            "request": {
                "identifier": dataset_id,
                "column_fields": ["column_name", "type", "is_dttm"],
            }
        })
 
    def execute_sql(self, database_id: int, sql: str, schema: str = "public") -> AgentResult:
        return self.call_tool("execute_sql", {
            "request": {
                "database_id": database_id,
                "sql": sql,
                "schema": schema,
                "limit": 100,
            }
        })
 
    def generate_chart(self, params: dict) -> AgentResult:
        """
        params should be a dict ready to pass as the "request" value.
        Shape: {"dataset_id": int, "config": {...}, "chart_name": str, "save_chart": True}
        """
        return self.call_tool("generate_chart", {"request": params})
 
    def list_charts(self) -> AgentResult:
        return self.call_tool("list_charts", {"request": {}})
 
    def get_chart_info(self, chart_id: int) -> AgentResult:
        return self.call_tool("get_chart_info", {
            "request": {"identifier": chart_id}
        })
 
    def generate_dashboard(self, params: dict) -> AgentResult:
        """
        params should be a dict ready to pass as the "request" value.
        Shape: {"chart_ids": [...], "dashboard_title": str, "published": True}
        """
        return self.call_tool("generate_dashboard", {"request": params})
 
    def get_dashboard_info(self, dashboard_id: int) -> AgentResult:
        return self.call_tool("get_dashboard_info", {
            "request": {"identifier": dashboard_id}
        })
 
    def close(self):
        self._http.close()
 
 
class MCPError(Exception):
    """Raised when MCP transport-level errors occur."""
    pass