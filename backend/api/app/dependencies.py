"""Composition root wiring: shared resources and per-request dependencies.

Long-lived clients (Mongo, Foundry) are created once during the lifespan and
stashed on ``app.state``. Creating them per request would re-run the OIDC
handshake and rebuild the connection pool.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.repositories.mongo import ConversationRepository, MongoResources
from app.services.agent_client import AgentClient
from app.services.conversation_service import ConversationService


def get_mongo_resources(request: Request) -> MongoResources:
    # isinstance rather than a None check: it narrows the type for mypy and is a
    # stronger guarantee that lifespan wiring actually ran.
    resources = getattr(request.app.state, "mongo", None)
    if not isinstance(resources, MongoResources):  # pragma: no cover - lifespan guarantees this
        raise RuntimeError("Mongo resources are not initialised.")
    return resources


def get_agent_client(request: Request) -> AgentClient:
    client = getattr(request.app.state, "agent_client", None)
    if not isinstance(client, AgentClient):  # pragma: no cover - lifespan guarantees this
        raise RuntimeError("Agent client is not initialised.")
    return client


def get_repository(
    resources: Annotated[MongoResources, Depends(get_mongo_resources)],
) -> ConversationRepository:
    return resources.repository()


def get_conversation_service(
    repository: Annotated[ConversationRepository, Depends(get_repository)],
    agent_client: Annotated[AgentClient, Depends(get_agent_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationService:
    return ConversationService(
        repository=repository,
        agent_client=agent_client,
        max_input_chars=settings.max_input_chars,
        max_output_chars=settings.max_output_chars,
        history_max_chars=settings.history_max_chars,
        history_max_messages=settings.history_max_messages,
        turn_lease_seconds=settings.turn_lease_seconds,
        foundry_conversation_agents=settings.foundry_conversation_agents,
    )


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
AgentClientDep = Annotated[AgentClient, Depends(get_agent_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
