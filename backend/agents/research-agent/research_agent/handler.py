"""Responses-protocol handler for the research agent.

Uses the framework-agnostic ``azure-ai-agentserver-responses`` adapter directly.
The adapter owns the HTTP surface: it binds port 8088, serves ``POST /responses``
and the ``GET /readiness`` health probe, and manages SSE streaming and the
response lifecycle. We only supply the business logic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from research_agent.gateway import (
    create_gateway_client,
    current_user_key,
    gateway_headers,
)
from research_agent.input import normalize_context_input
from research_agent.runtime import create_completion

if TYPE_CHECKING:
    from research_agent.config import AgentConfig

_LOGGER = logging.getLogger("research_agent.handler")


def create_app(config: AgentConfig) -> ResponsesAgentServerHost:
    """Build the agent server host with its handler bound to ``config``.

    Returned rather than module-global so the wiring stays testable and the
    composition root (``main.py``) owns process lifetime.
    """
    # Keyless auth. In Foundry this resolves to the per-agent Entra identity that
    # the platform provisions at deploy time; locally it resolves to the signed-in
    # developer. No API keys are used anywhere.
    credential = DefaultAzureCredential()
    resources = AsyncExitStack()
    resources.push_async_callback(credential.close)

    # Gateway mode sends model traffic through API Management, which is what
    # produces per-user token and cost telemetry. Direct mode is retained so the
    # agent still runs where no gateway is provisioned.
    if config.model_gateway_url is not None:
        openai_client = create_gateway_client(
            config.model_gateway_url,
            config.request_timeout_seconds,
        )
        resources.push_async_callback(openai_client.close)
    else:
        project_client = AIProjectClient(
            endpoint=config.project_endpoint,
            credential=credential,
        )
        resources.push_async_callback(project_client.close)
        openai_client = project_client.get_openai_client(
            timeout=config.request_timeout_seconds,
        )
        resources.push_async_callback(openai_client.close)

    responses_client = openai_client.responses

    app = ResponsesAgentServerHost()

    adapter_shutdown = _registered_shutdown_handler(app)

    @app.shutdown_handler  # type: ignore[misc]
    async def shutdown() -> None:
        try:
            if adapter_shutdown is not None:
                await adapter_shutdown()
        finally:
            await resources.aclose()

    # The preview adapter ships no stubs, so the decorator is untyped from mypy's
    # point of view. The handler itself below is fully annotated.
    @app.response_handler  # type: ignore[misc]
    async def handler(
        request: CreateResponse,
        context: ResponseContext,
        cancellation_signal: asyncio.Event,
    ) -> TextResponse:
        input_items = await normalize_context_input(context, config.max_input_chars)
        input_chars = sum(len(item["content"]) for item in input_items)
        user_key = current_user_key()

        # Log shape and the pseudonymous key, never content.
        _LOGGER.info(
            "handling response request",
            extra={
                "input_chars": input_chars,
                "input_messages": len(input_items),
                "user_key": user_key,
            },
        )

        if not any(item["content"].strip() for item in input_items):
            return TextResponse(
                context,
                request,
                text="I did not receive any input. What would you like to know?",
            )

        headers = (
            await gateway_headers(credential, user_key)
            if config.model_gateway_url is not None
            else None
        )

        result = await create_completion(
            responses_client,
            model=config.model_deployment_name,
            input_items=input_items,
            max_output_tokens=config.max_output_tokens,
            cancellation_signal=cancellation_signal,
            shutdown_signal=context.shutdown,
            extra_headers=headers,
        )

        return TextResponse(context, request, text=result)

    return app


def _registered_shutdown_handler(
    app: ResponsesAgentServerHost,
) -> Callable[[], Awaitable[None]] | None:
    """Read the handler installed by the preview adapter so cleanup can compose with it."""
    handler = getattr(app, "_shutdown_fn", None)
    return handler if callable(handler) else None
