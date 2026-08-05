"""Agent discovery endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import AgentClientDep
from app.domain.models import AgentSummary
from app.security.auth import Principal, get_principal

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(
    agent_client: AgentClientDep,
    _principal: Annotated[Principal, Depends(get_principal)],
) -> list[AgentSummary]:
    """List the agents this API is configured to talk to.

    The list comes from `AGENT_NAMES`, not from the Foundry project, so the client
    can only ever address agents an operator has explicitly allowed.
    """
    return [AgentSummary(name=name) for name in agent_client.agent_names]
