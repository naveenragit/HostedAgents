"""Authentication and caller identity.

The user id is derived from a validated Entra ID token — never from a client-supplied
field. Trusting a request body or query parameter for `userId` would let any caller
read any other user's history (OWASP A01: Broken Access Control / IDOR).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Final

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import AuthMode, Settings, get_settings

_LOGGER = logging.getLogger("api.security")

_DEV_USER_HEADER: Final[str] = "x-dev-user-id"
_DEV_FALLBACK_USER: Final[str] = "dev-user"

# Entra ID v2.0 access tokens are RS256.
_ALGORITHMS: Final[list[str]] = ["RS256"]

# HTTPBearer with auto_error=False so we can emit a consistent 401 body rather
# than FastAPI's default, and so dev mode can skip the header entirely.
_bearer = HTTPBearer(auto_error=False)

_jwks_clients: dict[str, PyJWKClient] = {}


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: str
    """Stable, non-reassignable user identifier. Used as the Cosmos partition key."""

    display_name: str | None = None


def _get_jwks_client(jwks_uri: str) -> PyJWKClient:
    """Return a cached JWKS client.

    `PyJWKClient` caches signing keys internally; reusing one instance per issuer
    avoids refetching the key set on every request.
    """
    client = _jwks_clients.get(jwks_uri)
    if client is None:
        client = PyJWKClient(jwks_uri, cache_keys=True)
        _jwks_clients[jwks_uri] = client
    return client


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build a principal from validated token claims.

    `oid` is the immutable object id of the user in the tenant. `sub` is only unique
    per application, and `email` / `preferred_username` are mutable and reassignable —
    none of them are safe as a durable partition key.
    """
    user_id = claims.get("oid")
    if not isinstance(user_id, str) or not user_id:
        raise _unauthorized("Token is missing the required 'oid' claim.")

    display_name = claims.get("name")
    return Principal(
        user_id=user_id,
        display_name=display_name if isinstance(display_name, str) else None,
    )


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """FastAPI dependency that resolves the authenticated caller."""
    if settings.auth_mode is AuthMode.DEV:
        # Reachable only on a developer workstation: `Settings` refuses AUTH_MODE=dev
        # when Azure compute markers are present.
        user_id = request.headers.get(_DEV_USER_HEADER, _DEV_FALLBACK_USER).strip()
        return Principal(user_id=user_id or _DEV_FALLBACK_USER, display_name=user_id)

    entra = settings.entra
    if entra is None:  # pragma: no cover - guarded by Settings construction
        raise _unauthorized("Entra configuration is missing.")

    if credentials is None or not credentials.credentials:
        raise _unauthorized("Missing bearer token.")

    try:
        signing_key = _get_jwks_client(entra.jwks_uri).get_signing_key_from_jwt(
            credentials.credentials
        )
        claims: dict[str, Any] = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=_ALGORITHMS,
            audience=entra.api_audience,
            issuer=entra.issuer,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        # Log the class of failure, never the token itself.
        _LOGGER.warning("token rejected", extra={"reason": type(exc).__name__})
        raise _unauthorized("Invalid or expired token.") from exc

    return _principal_from_claims(claims)
