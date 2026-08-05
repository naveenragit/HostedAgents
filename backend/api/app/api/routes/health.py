"""Liveness and readiness probes.

Unauthenticated by design: probes run before any token is available. They expose
no user data and no configuration values.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.domain.models import HealthResponse

_LOGGER = logging.getLogger("api.health")

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> HealthResponse:
    """Process is up. Does not touch dependencies."""
    return HealthResponse(status="ok")


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Verify the database connection before declaring the service ready."""
    mongo = getattr(request.app.state, "mongo", None)
    if mongo is None:
        return _not_ready("database client not initialised")

    try:
        await mongo.ping()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        # Log the failure class only; the message can carry endpoint detail.
        _LOGGER.warning("readiness check failed", extra={"error_type": type(exc).__name__})
        return _not_ready("database unreachable")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=HealthResponse(status="ready", detail={"database": "ok"}).model_dump(),
    )


def _not_ready(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=HealthResponse(status="not_ready", detail={"reason": reason}).model_dump(),
    )
