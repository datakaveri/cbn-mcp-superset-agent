"""
Superset authentication — simple admin/admin login.
Gets a Bearer token for REST API fallback calls.
"""

import json
import logging
import requests

from config import (
    SUPERSET_API_URL, SUPERSET_BASE_URL,
    SUPERSET_USERNAME, SUPERSET_PASSWORD, REQUEST_TIMEOUT,
)
from models import AgentResult

log = logging.getLogger(__name__)


class SupersetAuth:
    """Handles Superset login and token caching."""

    def __init__(self):
        self._token: str | None = None
        self._http = requests.Session()
        self._last_error: str | None = None

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def last_error(self) -> str | None:
        """Reason the most recent _api_post failed (for surfacing to callers)."""
        return self._last_error

    @property
    def headers(self) -> dict:
        """Headers with Bearer token for REST API calls."""
        if not self._token:
            raise RuntimeError("Not authenticated — call login() first")
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def login(self) -> AgentResult:
        """
        Authenticate with Superset and cache the token.
        Returns AgentResult with the token on success.
        """
        url = f"{SUPERSET_API_URL}/security/login"
        payload = {
            "username": SUPERSET_USERNAME,
            "password": SUPERSET_PASSWORD,
            "provider": "db",
            "refresh": True,
        }

        try:
            resp = self._http.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("access_token")
            if not self._token:
                return AgentResult.fail("No access_token in login response")
            log.info("Superset login successful")
            return AgentResult.ok(self._token)
        except requests.RequestException as e:
            return AgentResult.fail(f"Superset login failed: {e}")

    def _csrf(self) -> str:
        """Fetch a CSRF token (shares the session cookie)."""
        r = self._http.get(
            f"{SUPERSET_API_URL}/security/csrf_token/",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("result") or ""

    def _api_post(self, path: str, body: dict):
        """
        Authenticated POST to a Superset API path, with one re-login retry on 401
        (the access token expires on a long-running server). Returns the Response
        or None on failure; on failure self.last_error holds the reason.
        """
        self._last_error = None
        for attempt in (1, 2):
            if not self._token:
                login = self.login()
                if not login.success:
                    self._last_error = login.error
                    return None
            try:
                resp = self._http.post(
                    f"{SUPERSET_API_URL}{path}",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "X-CSRFToken": self._csrf(),
                        "Content-Type": "application/json",
                        "Referer": SUPERSET_BASE_URL,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 401 and attempt == 1:
                    self._token = None   # expired — re-login and retry once
                    continue
                if not resp.ok:
                    self._last_error = f"Superset {path} → HTTP {resp.status_code}: {resp.text[:300]}".strip()
                    log.warning("%s", self._last_error)
                    return None
                return resp
            except requests.RequestException as e:
                self._last_error = f"Superset {path} request error: {e}"
                # The token may be stale — e.g. _csrf() 401s with an expired token
                # (raised before the POST, so the status-code retry above never
                # fires). Clear it and re-login on the retry.
                if attempt == 1:
                    self._token = None
                    continue
                log.warning("%s", self._last_error)
                return None
        return None

    def register_embedding(self, dashboard_id, allowed_domains=None):
        """
        Register a dashboard for embedding (POST /dashboard/{id}/embedded) so the
        guest-token preview's /embedded/<uuid> page resolves. Returns the embedded
        uuid, or None on failure (non-fatal).
        """
        resp = self._api_post(
            f"/dashboard/{dashboard_id}/embedded",
            {"allowed_domains": allowed_domains or []},
        )
        if resp is None:
            log.warning("Embed registration failed for dashboard %s (check SUPERSET creds)", dashboard_id)
            return None
        uuid = (resp.json().get("result") or {}).get("uuid")
        log.info("Registered dashboard %s for embedding (embed uuid=%s)", dashboard_id, uuid)
        return uuid

    def create_chart(self, slice_name, viz_type, dataset_id, form_data, query_context):
        """
        Create a chart via Superset's REST API with a raw viz_type + form_data.
        This is the fallback for chart types the MCP's generate_chart can't render
        (box_plot, treemap, sunburst, funnel, waterfall, …). Storing query_context
        makes it render deterministically. Returns the new chart id, or None.
        """
        resp = self._api_post("/chart/", {
            "slice_name": slice_name,
            "viz_type": viz_type,
            "datasource_id": int(dataset_id),
            "datasource_type": "table",
            "params": json.dumps(form_data),
            "query_context": json.dumps(query_context),
        })
        if resp is None:
            return None
        return (resp.json() or {}).get("id")

    def mint_guest_token(self, resource_uuid, rls=None):
        """
        Mint a Superset guest token scoped to an embedded dashboard, so the agent
        UI can render it inline without the viewer owning the dashboard. Returns
        the token string, or None on failure.
        """
        resp = self._api_post("/security/guest_token/", {
            "user": {"username": "embed_viewer", "first_name": "Embed", "last_name": "Viewer"},
            "resources": [{"type": "dashboard", "id": resource_uuid}],
            "rls": rls or [],
        })
        if resp is None:
            return None
        return resp.json().get("token")

    def close(self):
        self._http.close()
