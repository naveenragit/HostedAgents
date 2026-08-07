"""Authenticated discovery of server-enforced client limits."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.domain.models import CapabilitiesResponse
from app.security.auth import Principal, get_principal

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get("")
async def get_capabilities(
    settings: Annotated[Settings, Depends(get_settings)],
    _principal: Annotated[Principal, Depends(get_principal)],
) -> CapabilitiesResponse:
    """Return limits the UI must enforce before submitting a request."""
    return CapabilitiesResponse(max_input_chars=settings.max_input_chars)
