"""
Keycloak JWT verification for the Flask web UI.

Validates bearer tokens against the realm's JWKS (signature + issuer + expiry)
so that protected endpoints can only be reached by authenticated users — even
if someone bypasses the browser and calls the API directly.

Exposes a `@require_auth` decorator for Flask routes.
"""

import logging
from functools import wraps

from config import (
    KEYCLOAK_ENABLED,
    KEYCLOAK_URL,
    KEYCLOAK_REALM,
    KEYCLOAK_REQUIRED_ROLE,
)

log = logging.getLogger(__name__)

ISSUER = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"

# PyJWT is only required when auth is enabled; import lazily so the rest of the
# app (TUI, headless) keeps working without the dependency.
try:
    import jwt
    from jwt import PyJWKClient
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

# JWKS keys are fetched once and cached by PyJWKClient.
_jwk_client = None


def _get_jwk_client():
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(JWKS_URL)
    return _jwk_client


class AuthError(Exception):
    """Raised when a token is missing, invalid, or lacks a required role."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def verify_token(token: str) -> dict:
    """Verify a Keycloak access token. Returns the claims dict or raises AuthError."""
    if not _JWT_AVAILABLE:
        raise AuthError("server auth misconfigured: PyJWT not installed", status=500)

    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            # Keycloak access tokens carry the client in `azp`, not `aud`, so we
            # verify signature/issuer/expiry and skip strict audience matching.
            options={"verify_aud": False},
        )
    except AuthError:
        raise
    except Exception as e:  # signature, expiry, issuer, JWKS fetch, etc.
        raise AuthError(f"invalid token: {e}") from e

    if KEYCLOAK_REQUIRED_ROLE:
        roles = (claims.get("realm_access") or {}).get("roles", [])
        if KEYCLOAK_REQUIRED_ROLE not in roles:
            raise AuthError(
                f"missing required role '{KEYCLOAK_REQUIRED_ROLE}'", status=403
            )

    return claims


def require_auth(f):
    """Flask decorator: reject requests without a valid Keycloak bearer token."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not KEYCLOAK_ENABLED:
            return f(*args, **kwargs)

        from flask import request, jsonify

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "authentication required"}), 401

        try:
            request.user = verify_token(auth[7:].strip())
        except AuthError as e:
            log.warning("Auth rejected: %s", e.message)
            return jsonify({"error": e.message}), e.status

        return f(*args, **kwargs)

    return wrapper
