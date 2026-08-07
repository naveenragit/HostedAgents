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
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.routes import api_router
from app.config import ConfigError, Settings, get_settings
from app.repositories.mongo import InvalidCursorError, MongoResources
from app.security.auth import require_local_request
from app.security.credentials import build_azure_credential
from app.services.agent_client import (
    AgentClient,
    AgentInvocationError,
    UnknownAgentError,
)
from app.services.conversation_service import (
    AgentOutputTooLongError,
    ConversationConflictError,
    ConversationNotFoundError,
    InputTooLongError,
    TurnPendingError,
)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_LOGGER = logging.getLogger("api")

# Resolved from this file, not the working directory, so `uvicorn app.main:app`
# behaves the same whether it is launched from backend/api or the repo root.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class _LocalOnlyMiddleware:
    """Apply the POC boundary before routing, including docs and health endpoints."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                require_local_request(Request(scope))
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


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
    credential = build_azure_credential(settings)

    mongo = MongoResources(
        connection_string=settings.mongo_connection_string,
        credential=credential,
        database_name=settings.mongo_database,
        conversations_collection=settings.conversations_collection,
        messages_collection=settings.messages_collection,
        turns_collection=settings.turns_collection,
        deletion_tombstone_retention_seconds=(settings.deletion_tombstone_retention_seconds),
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
            "poc_mode": settings.poc_mode,
            "agents": ",".join(settings.agent_names),
            "database": settings.mongo_database,
        },
    )

    try:
        yield
    finally:
        await mongo.close()
        project_client.close()
        credential.close()
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

    @app.exception_handler(InvalidCursorError)
    async def _invalid_cursor(_: Request, exc: InvalidCursorError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid pagination cursor."},
        )

    @app.exception_handler(InputTooLongError)
    async def _input_too_long(_: Request, exc: InputTooLongError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": str(exc)},
        )

    @app.exception_handler(TurnPendingError)
    async def _turn_pending(_: Request, exc: TurnPendingError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(ConversationConflictError)
    async def _conversation_conflict(_: Request, exc: ConversationConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
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

    @app.exception_handler(AgentOutputTooLongError)
    async def _agent_output_too_long(_: Request, exc: AgentOutputTooLongError) -> JSONResponse:
        _LOGGER.error("agent output rejected", extra={"error_type": type(exc).__name__})
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "The agent returned an invalid response. Please retry."},
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
        # Explicit allow-list only. The POC uses no browser credentials.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
            ],
            max_age=600,
        )

    # Added after CORS so this guard is outermost and also covers preflight requests.
    app.add_middleware(_LocalOnlyMiddleware)

    _register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
