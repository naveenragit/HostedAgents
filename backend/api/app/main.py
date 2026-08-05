"""FastAPI application factory.

Composition root for the service layer. Long-lived clients are created once in the
lifespan and disposed on shutdown; domain errors are translated to HTTP status
codes here so route handlers stay free of transport concerns.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router
from app.config import ConfigError, Settings, get_settings
from app.repositories.mongo import MongoResources
from app.services.agent_client import (
    AgentClient,
    AgentInvocationError,
    UnknownAgentError,
)
from app.services.conversation_service import ConversationNotFoundError

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_LOGGER = logging.getLogger("api")

# Resolved from this file, not the working directory, so `uvicorn app.main:app`
# behaves the same whether it is launched from backend/api or the repo root.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _configure_logging(level: str) -> None:
    """PII-safe logging: shapes, ids and outcomes — never message bodies."""
    resolved = getattr(logging, level, logging.INFO)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    logging.basicConfig(level=resolved, format=_LOG_FORMAT, stream=sys.stdout, force=True)
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared clients on start-up, dispose them on shutdown."""
    settings: Settings = app.state.settings

    # Keyless throughout. One synchronous credential serves both clients: the
    # Foundry SDK is synchronous, and PyMongo's OIDC callback is invoked
    # synchronously by the driver.
    credential = DefaultAzureCredential()

    mongo = MongoResources(
        connection_string=settings.mongo_connection_string,
        credential=credential,
        database_name=settings.mongo_database,
        conversations_collection=settings.conversations_collection,
        messages_collection=settings.messages_collection,
    )

    # Creates the database, collections and indexes on first run. Idempotent, so
    # it is safe on every start.
    await mongo.ensure_schema()

    project_client = AIProjectClient(
        endpoint=settings.foundry_project_endpoint,
        credential=credential,
    )
    agent_client = AgentClient(
        project_client=project_client,
        allowed_agents=settings.agent_names,
        timeout_seconds=settings.request_timeout_seconds,
    )

    app.state.mongo = mongo
    app.state.agent_client = agent_client

    _LOGGER.info(
        "api started",
        extra={
            "auth_mode": settings.auth_mode.value,
            "agents": ",".join(settings.agent_names),
            "database": settings.mongo_database,
        },
    )

    try:
        yield
    finally:
        await mongo.close()
        _LOGGER.info("api stopped")


def _register_exception_handlers(app: FastAPI) -> None:
    """Map domain errors to HTTP responses.

    Internal failure detail is logged, never returned: an error body is an
    information-disclosure surface.
    """

    @app.exception_handler(ConversationNotFoundError)
    async def _not_found(_: Request, exc: ConversationNotFoundError) -> JSONResponse:
        # Same response whether the conversation is missing or owned by someone
        # else — the client must not be able to probe for foreign ids.
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Conversation not found."},
        )

    @app.exception_handler(UnknownAgentError)
    async def _unknown_agent(_: Request, exc: UnknownAgentError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.exception_handler(AgentInvocationError)
    async def _agent_failed(_: Request, exc: AgentInvocationError) -> JSONResponse:
        _LOGGER.error("agent invocation failed", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "The agent is currently unavailable. Please retry."},
        )


def create_app() -> FastAPI:
    """Build the configured application."""
    # Real environment variables win over the file, so a deployed environment is
    # never overridden by a stray local .env.
    load_dotenv(_ENV_FILE, override=False)

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        raise

    _configure_logging(settings.log_level)

    app = FastAPI(
        title="Hosted Agents API",
        version="0.1.0",
        summary="Conversation API over Microsoft Foundry hosted agents.",
        lifespan=_lifespan,
    )
    app.state.settings = settings

    if settings.cors_allowed_origins:
        # Explicit allow-list only, and credentials are enabled — which the CORS
        # spec forbids combining with a "*" origin. `Settings` never derives one.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "x-dev-user-id"],
            max_age=600,
        )

    _register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
