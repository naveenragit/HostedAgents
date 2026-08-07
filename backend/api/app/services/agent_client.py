"""Client for the deployed Foundry hosted agents.

Wraps ``AIProjectClient.get_openai_client(agent_name=...)``, the supported way to
invoke a hosted agent over the Responses protocol. Callers can either supply a
Foundry conversation id or use the stateless path with caller-managed history.

The agent allow-list is configuration, not caller input: a client can only reach an
agent named in ``AGENT_NAMES``. Without that, a request body could point the service
at an arbitrary agent in the project.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

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
    user_message_id: str | None = None
    assistant_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentConversationMessage:
    """One renderable text message stored in a Foundry conversation."""

    id: str
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class AgentConversationPage:
    """One reverse-cursor page projected from Foundry conversation items."""

    items: list[AgentConversationMessage]
    next_cursor: str | None


class AgentInputItem(TypedDict):
    """One chronological message supplied to a stateless agent invocation."""

    role: Literal["user", "assistant"]
    content: str


class AgentClient:
    """Invokes hosted agents with Foundry-managed or caller-managed context."""

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
        """Create a durable Responses conversation on one hosted-agent endpoint."""
        self.ensure_known(agent_name)

        def create() -> Any:
            client = self._openai_client(agent_name)
            try:
                return client.conversations.create()
            finally:
                client.close()

        conversation = await self._run(create)
        conversation_id = getattr(conversation, "id", None)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise AgentInvocationError(f"Agent {agent_name!r} returned an invalid conversation id.")
        return conversation_id

    async def delete_conversation(self, agent_name: str, conversation_id: str) -> None:
        """Delete a Foundry conversation; an already-absent object is success."""
        self.ensure_known(agent_name)

        def delete() -> None:
            client = self._openai_client(agent_name)
            try:
                try:
                    client.conversations.delete(conversation_id)
                except Exception as exc:
                    if getattr(exc, "status_code", None) != 404:
                        raise
            finally:
                client.close()

        await self._run(delete)

    async def list_conversation_messages(
        self,
        agent_name: str,
        conversation_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> AgentConversationPage:
        """List Foundry-owned user/assistant text items, newest page first."""
        self.ensure_known(agent_name)

        def list_items() -> AgentConversationPage:
            client = self._openai_client(agent_name)
            try:
                remaining = limit
                after = cursor
                messages: list[AgentConversationMessage] = []
                has_more = False
                next_cursor: str | None = None

                # Tool calls are valid conversation items but are not chat bubbles.
                # Scan bounded pages until the requested number of messages is met.
                for _ in range(10):
                    request: dict[str, Any] = {
                        "order": "desc",
                        "limit": remaining,
                    }
                    if after is not None:
                        request["after"] = after
                    page = client.conversations.items.list(conversation_id, **request)
                    for item in page.data:
                        message = _project_conversation_message(item)
                        if message is not None:
                            messages.append(message)

                    has_more = bool(page.has_more)
                    next_cursor = page.last_id if has_more else None
                    remaining = limit - len(messages)
                    if remaining <= 0 or not has_more:
                        break
                    if (
                        not isinstance(page.last_id, str)
                        or not page.last_id
                        or page.last_id == after
                    ):
                        raise AgentInvocationError("Foundry returned a non-advancing item cursor.")
                    after = page.last_id

                return AgentConversationPage(
                    items=list(reversed(messages)),
                    next_cursor=next_cursor if has_more else None,
                )
            finally:
                client.close()

        return cast(AgentConversationPage, await self._run(list_items))

    async def get_conversation_messages(
        self,
        agent_name: str,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
    ) -> tuple[AgentConversationMessage, AgentConversationMessage]:
        """Retrieve a previously completed Foundry turn for idempotent replay."""
        self.ensure_known(agent_name)

        def retrieve() -> tuple[AgentConversationMessage, AgentConversationMessage]:
            client = self._openai_client(agent_name)
            try:
                user_item = client.conversations.items.retrieve(
                    user_message_id,
                    conversation_id=conversation_id,
                )
                assistant_item = client.conversations.items.retrieve(
                    assistant_message_id,
                    conversation_id=conversation_id,
                )
                user_message = _project_conversation_message(user_item)
                assistant_message = _project_conversation_message(assistant_item)
                if user_message is None or user_message.role != "user":
                    raise AgentInvocationError("Foundry replay is missing its user message.")
                if assistant_message is None or assistant_message.role != "assistant":
                    raise AgentInvocationError("Foundry replay is missing its assistant message.")
                return user_message, assistant_message
            finally:
                client.close()

        return cast(
            tuple[AgentConversationMessage, AgentConversationMessage],
            await self._run(retrieve),
        )

    async def send(
        self,
        agent_name: str,
        input_items: list[AgentInputItem],
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        """Send a turn using either Foundry-managed or caller-managed history."""
        self.ensure_known(agent_name)

        def invoke() -> Any:
            client = self._openai_client(agent_name)
            try:
                request: dict[str, Any] = {"input": input_items}
                if conversation_id is None:
                    request["store"] = False
                else:
                    request["conversation"] = conversation_id
                response = client.responses.create(**request)
                if conversation_id is None:
                    return response, None, None

                output_ids = {
                    item_id
                    for item in getattr(response, "output", [])
                    if isinstance((item_id := getattr(item, "id", None)), str)
                }
                page = client.conversations.items.list(
                    conversation_id,
                    order="desc",
                    limit=100,
                )
                projected = [
                    message
                    for item in page.data
                    if (message := _project_conversation_message(item)) is not None
                ]
                assistant_index = next(
                    (
                        index
                        for index, message in enumerate(projected)
                        if message.role == "assistant" and message.id in output_ids
                    ),
                    None,
                )
                if assistant_index is None:
                    raise AgentInvocationError(
                        "Foundry conversation did not contain the completed assistant item."
                    )
                user_message = next(
                    (
                        message
                        for message in projected[assistant_index + 1 :]
                        if message.role == "user"
                    ),
                    None,
                )
                if user_message is None:
                    raise AgentInvocationError(
                        "Foundry conversation did not contain the submitted user item."
                    )
                return response, user_message.id, projected[assistant_index].id
            finally:
                client.close()

        response, user_message_id, assistant_message_id = await self._run(invoke)

        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise AgentInvocationError(f"Agent {agent_name!r} returned an empty response.")

        response_id = getattr(response, "id", None)
        return AgentReply(
            text=text,
            response_id=response_id if isinstance(response_id, str) else None,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )

    def _openai_client(self, agent_name: str) -> Any:
        # Responses.create is not documented as idempotent. SDK retries could
        # therefore dispatch the same turn twice after an ambiguous transport
        # failure, so this boundary deliberately permits only one HTTP attempt.
        return self._project.get_openai_client(
            agent_name=agent_name,
            max_retries=0,
            timeout=self._timeout_seconds,
        )

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


def _project_conversation_message(item: Any) -> AgentConversationMessage | None:
    """Project one heterogeneous Foundry item into the API's text-chat shape."""
    if getattr(item, "type", None) != "message":
        return None
    role = getattr(item, "role", None)
    item_id = getattr(item, "id", None)
    if role not in {"user", "assistant"} or not isinstance(item_id, str) or not item_id:
        return None

    fragments: list[str] = []
    for part in getattr(item, "content", []):
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            fragments.append(text)
            continue
        refusal = getattr(part, "refusal", None)
        if isinstance(refusal, str) and refusal:
            fragments.append(refusal)
    if not fragments:
        return None

    return AgentConversationMessage(
        id=item_id,
        role=role,
        content="\n".join(fragments),
    )
