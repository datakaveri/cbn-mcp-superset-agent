"""
Superset authentication — simple admin/admin login.
Gets a Bearer token for REST API fallback calls.
"""

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

    @property
    def token(self) -> str | None:
        return self._token

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

    def register_embedding(self, dashboard_id, allowed_domains=None):
        """
        Register a dashboard for embedding (POST /dashboard/{id}/embedded) so the
        guest-token preview's /embedded/<uuid> page resolves. Returns the embedded
        uuid, or None on failure (non-fatal). Logs in on demand.
        """
        if not self._token and not self.login().success:
            log.warning("Embed registration skipped — Superset login failed (check SUPERSET_USERNAME/PASSWORD)")
            return None

        try:
            csrf_resp = self._http.get(
                f"{SUPERSET_API_URL}/security/csrf_token/",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=REQUEST_TIMEOUT,
            )
            csrf_resp.raise_for_status()
            csrf = csrf_resp.json().get("result")

            resp = self._http.post(
                f"{SUPERSET_API_URL}/dashboard/{dashboard_id}/embedded",
                json={"allowed_domains": allowed_domains or []},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-CSRFToken": csrf or "",
                    "Content-Type": "application/json",
                    "Referer": SUPERSET_BASE_URL,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            uuid = (resp.json().get("result") or {}).get("uuid")
            log.info("Registered dashboard %s for embedding (embed uuid=%s)", dashboard_id, uuid)
            return uuid
        except requests.RequestException as e:
            log.warning("Embed registration failed for dashboard %s: %s", dashboard_id, e)
            return None

    def close(self):
        self._http.close()
