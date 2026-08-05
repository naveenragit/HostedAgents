"""Responses-protocol handler for the research agent.

Uses the framework-agnostic ``azure-ai-agentserver-responses`` adapter directly.
The adapter owns the HTTP surface: it binds port 8088, serves ``POST /responses``
and the ``GET /readiness`` health probe, and manages SSE streaming and the
response lifecycle. We only supply the business logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from research_agent.instructions import SYSTEM_INSTRUCTIONS

if TYPE_CHECKING:
    from research_agent.config import AgentConfig

_LOGGER = logging.getLogger("research_agent.handler")

# Number of prior turns the adapter hydrates from platform-managed history.
_HISTORY_TURNS = 20

_TRUNCATION_NOTICE = "\n\n[Input truncated to the configured maximum length.]"


def create_app(config: AgentConfig) -> ResponsesAgentServerHost:
    """Build the agent server host with its handler bound to ``config``.

    Returned rather than module-global so the wiring stays testable and the
    composition root (``main.py``) owns process lifetime.
    """
    # Keyless auth. In Foundry this resolves to the per-agent Entra identity that
    # the platform provisions at deploy time; locally it resolves to the signed-in
    # developer. No API keys are used anywhere.
    credential = DefaultAzureCredential()
    project_client = AIProjectClient(
        endpoint=config.project_endpoint,
        credential=credential,
    )
    responses_client = project_client.get_openai_client().responses

    app = ResponsesAgentServerHost(
        options=ResponsesServerOptions(default_fetch_history_count=_HISTORY_TURNS),
    )

    # The preview adapter ships no stubs, so the decorator is untyped from mypy's
    # point of view. The handler itself below is fully annotated.
    @app.response_handler  # type: ignore[misc]
    async def handler(
        request: CreateResponse,
        context: ResponseContext,
        _cancellation_signal: asyncio.Event,
    ) -> TextResponse:
        raw_input = await context.get_input_text() or ""
        user_input = _bound_input(raw_input, config.max_input_chars)

        # Log shape, never content.
        _LOGGER.info(
            "handling response request",
            extra={"input_chars": len(user_input), "truncated": len(raw_input) > len(user_input)},
        )

        if not user_input.strip():
            return TextResponse(
                context,
                request,
                text="I did not receive any input. What would you like to know?",
            )

        result = await asyncio.to_thread(
            _create_completion,
            responses_client,
            config.model_deployment_name,
            user_input,
        )

        return TextResponse(context, request, text=result)

    return app


def _create_completion(responses_client: Any, model: str, user_input: str) -> str:
    """Call the model synchronously; invoked on a worker thread."""
    response = responses_client.create(
        model=model,
        instructions=SYSTEM_INSTRUCTIONS,
        input=[{"role": "user", "content": user_input}],
        # Conversation history is managed by the hosting platform, so the agent
        # does not persist any conversation content itself.
        store=False,
    )
    return str(response.output_text)


def _bound_input(text: str, max_chars: int) -> str:
    """Cap user input length to bound token cost and prompt-stuffing."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_NOTICE
