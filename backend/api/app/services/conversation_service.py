"""Conversation orchestration.

This is the single writer for chat history. Keeping persistence here — rather than
inside each agent — means:

* one place enforces per-user partitioning, so there is one thing to audit;
* the agents stay stateless and need no Cosmos RBAC on their per-agent Entra
  identities (which only exist *after* the first deploy, and would otherwise force
  a deploy -> assign-role -> redeploy cycle);
* persistence logic is not duplicated across every agent added later.

If an agent ever needs to read history mid-turn, expose it as a read-only tool
backed by this service rather than giving the agent its own database credentials.
"""

from __future__ import annotations

import logging

from app.domain.models import Conversation, Message, Role
from app.repositories.mongo import ConversationRepository
from app.services.agent_client import AgentClient

_LOGGER = logging.getLogger("api.conversations")

_TITLE_MAX_CHARS = 60


class ConversationNotFoundError(LookupError):
    """Raised when a conversation does not exist for the requesting user."""


class ConversationService:
    """Coordinates the agent client and the repository for one request."""

    def __init__(
        self,
        repository: ConversationRepository,
        agent_client: AgentClient,
        max_input_chars: int,
    ) -> None:
        self._repository = repository
        self._agents = agent_client
        self._max_input_chars = max_input_chars

    async def create_conversation(
        self,
        user_id: str,
        agent_name: str,
        title: str,
    ) -> Conversation:
        """Create a conversation for the user and its platform-managed counterpart."""
        self._agents.ensure_known(agent_name)
        foundry_conversation_id = await self._agents.create_conversation(agent_name)

        conversation = Conversation(
            user_id=user_id,
            agent_name=agent_name,
            title=title.strip()[:_TITLE_MAX_CHARS] or "New conversation",
            foundry_conversation_id=foundry_conversation_id,
        )

        await self._repository.create_conversation(conversation)
        _LOGGER.info(
            "conversation created",
            extra={"conversation_id": conversation.id, "agent": agent_name},
        )
        return conversation

    async def list_conversations(self, user_id: str) -> list[Conversation]:
        return await self._repository.list_conversations(user_id)

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation:
        conversation = await self._repository.get_conversation(user_id, conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def list_messages(self, user_id: str, conversation_id: str) -> list[Message]:
        # Resolve the conversation first so a request for someone else's id returns
        # 404 rather than an empty list, which would confirm the id exists.
        await self.get_conversation(user_id, conversation_id)
        return await self._repository.list_messages(user_id, conversation_id)

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        deleted = await self._repository.delete_conversation(user_id, conversation_id)
        if not deleted:
            raise ConversationNotFoundError(conversation_id)

    async def send_message(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
    ) -> tuple[Message, Message]:
        """Persist the user turn, invoke the agent, persist the reply.

        Returns the (user, assistant) message pair.
        """
        conversation = await self.get_conversation(user_id, conversation_id)
        bounded = self._bound(content)

        user_message = Message(
            user_id=user_id,
            conversation_id=conversation.id,
            role=Role.USER,
            content=bounded,
        )
        await self._repository.add_message(user_message)

        reply = await self._agents.send(
            agent_name=conversation.agent_name,
            content=bounded,
            conversation_id=conversation.foundry_conversation_id,
        )

        assistant_message = Message(
            user_id=user_id,
            conversation_id=conversation.id,
            role=Role.ASSISTANT,
            content=reply.text,
            response_id=reply.response_id,
        )
        await self._repository.add_message(assistant_message)

        await self._touch(conversation, first_user_turn=bounded)

        _LOGGER.info(
            "turn completed",
            extra={
                "conversation_id": conversation.id,
                "agent": conversation.agent_name,
                "input_chars": len(bounded),
                "output_chars": len(reply.text),
            },
        )
        return user_message, assistant_message

    async def _touch(self, conversation: Conversation, first_user_turn: str) -> None:
        """Update conversation counters, and derive a title from the opening turn."""
        updated = conversation.model_copy(
            update={
                "message_count": conversation.message_count + 2,
                "updated_at": Message(
                    user_id=conversation.user_id,
                    conversation_id=conversation.id,
                    role=Role.USER,
                    content="",
                ).created_at,
            }
        )

        if conversation.message_count == 0 and conversation.title == "New conversation":
            updated = updated.model_copy(update={"title": _derive_title(first_user_turn)})

        await self._repository.update_conversation(updated)

    def _bound(self, content: str) -> str:
        """Cap input length to bound token cost and prompt-stuffing."""
        stripped = content.strip()
        if len(stripped) <= self._max_input_chars:
            return stripped
        return stripped[: self._max_input_chars]


def _derive_title(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _TITLE_MAX_CHARS:
        return collapsed or "New conversation"
    return collapsed[: _TITLE_MAX_CHARS - 1].rstrip() + "\u2026"
