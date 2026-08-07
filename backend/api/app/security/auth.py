"""Loopback-only caller boundary for the local POC API.

The POC deliberately has no application-user authentication. Every request uses one
server-owned identity so clients cannot choose a MongoDB partition. Non-loopback peers
are rejected; this boundary must be replaced before deploying the API anywhere shared.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class Principal:
    """The fixed server-owned identity used to scope POC data."""

    user_id: str
    """Server configuration value used as the MongoDB partition key."""

    display_name: str | None = None


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


def require_local_request(request: Request) -> None:
    """Require both a loopback peer and a loopback HTTP Host header."""
    client = request.client
    if client is None:
        raise _forbidden("The POC API requires a loopback client.")
    try:
        is_loopback = ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise _forbidden("The unauthenticated POC API is available only over loopback.")

    host_header = request.headers.get("host", "")
    if not host_header or "@" in host_header:
        raise _forbidden("The POC API requires a loopback Host header.")
    try:
        hostname = urlsplit(f"//{host_header}").hostname
        host_is_loopback = hostname == "localhost" or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError:
        host_is_loopback = False
    if not host_is_loopback:
        raise _forbidden("The POC API requires a loopback Host header.")


async def get_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """Return the fixed POC identity after enforcing the local-only boundary."""
    require_local_request(request)
    return Principal(user_id=settings.poc_user_id, display_name="Local POC")
