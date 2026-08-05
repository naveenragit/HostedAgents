"""Client for the deployed Foundry hosted agents.

Wraps ``AIProjectClient.get_openai_client(agent_name=...)``, the supported way to
invoke a hosted agent over the Responses protocol. Multi-turn context is threaded
through a platform-managed conversation id, so the transcript we keep in Cosmos is
a durable per-user record rather than the mechanism the model relies on.

The agent allow-list is configuration, not caller input: a client can only reach an
agent named in ``AGENT_NAMES``. Without that, a request body could point the service
at an arbitrary agent in the project.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from azure.ai.projects import AIProjectClient

_LOGGER = logging.getLogger("api.agents")


class UnknownAgentError(ValueError):
    """Raised when a caller names an agent that is not configured."""


class AgentInvocationError(RuntimeError):
    """Raised when the agent could not produce a response."""


@dataclass(frozen=True, slots=True)
class AgentReply:
    """A completed agent turn."""

    text: str
    response_id: str | None


class AgentClient:
    """Invokes hosted agents and manages platform conversations."""

    def __init__(
        self,
        project_client: AIProjectClient,
        allowed_agents: tuple[str, ...],
        timeout_seconds: float,
    ) -> None:
        self._project = project_client
        self._allowed_agents = frozenset(allowed_agents)
        self._timeout_seconds = timeout_seconds

    @property
    def agent_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed_agents))

    def ensure_known(self, agent_name: str) -> str:
        """Validate an agent name against the allow-list."""
        if agent_name not in self._allowed_agents:
            raise UnknownAgentError(
                f"Unknown agent {agent_name!r}. Configured agents: "
                f"{', '.join(sorted(self._allowed_agents))}."
            )
        return agent_name

    async def create_conversation(self, agent_name: str) -> str:
        """Create a platform-managed conversation and return its id."""
        self.ensure_known(agent_name)
        client = self._openai_client(agent_name)

        conversation = await self._run(lambda: client.conversations.create())
        return str(conversation.id)

    async def send(
        self,
        agent_name: str,
        content: str,
        conversation_id: str | None,
    ) -> AgentReply:
        """Send one turn to the agent and return its reply."""
        self.ensure_known(agent_name)
        client = self._openai_client(agent_name)

        kwargs: dict[str, Any] = {"input": content}
        if conversation_id:
            kwargs["conversation"] = conversation_id

        response = await self._run(lambda: client.responses.create(**kwargs))

        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise AgentInvocationError(f"Agent {agent_name!r} returned an empty response.")

        response_id = getattr(response, "id", None)
        return AgentReply(
            text=text,
            response_id=response_id if isinstance(response_id, str) else None,
        )

    def _openai_client(self, agent_name: str) -> Any:
        # Bound per call: the client is cheap and binding it to the agent keeps the
        # invocation explicit rather than relying on shared mutable state.
        return self._project.get_openai_client(agent_name=agent_name)

    async def _run(self, call: Any) -> Any:
        """Run a blocking SDK call off the event loop, with a hard timeout.

        The Foundry SDK is synchronous. Calling it directly from an async handler
        would block the event loop for the whole model round-trip.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(call),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise AgentInvocationError(
                f"Agent call timed out after {self._timeout_seconds:.0f}s."
            ) from exc
        except Exception as exc:
            _LOGGER.exception("agent call failed", extra={"error_type": type(exc).__name__})
            raise AgentInvocationError("The agent could not be reached.") from exc
