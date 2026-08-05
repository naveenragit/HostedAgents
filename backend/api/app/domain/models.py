"""Domain models.

These are the API contract and the Cosmos document shape. Keeping them in one
place means a field can't drift between the wire format and storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

DOC_TYPE_CONVERSATION = "conversation"
DOC_TYPE_MESSAGE = "message"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


class Role(str, Enum):
    """Author of a message."""

    USER = "user"
    ASSISTANT = "assistant"


class Conversation(BaseModel):
    """A chat thread belonging to exactly one user.

    `user_id` is the Cosmos partition key. Every read and write is scoped to the
    authenticated caller's partition, so cross-user access is structurally
    impossible rather than merely checked.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=_new_id)
    user_id: str = Field(alias="userId")
    agent_name: str = Field(alias="agentName")
    title: str = Field(default="New conversation", max_length=200)
    # Foundry's platform-managed conversation, used to thread multi-turn context.
    foundry_conversation_id: str | None = Field(default=None, alias="foundryConversationId")
    created_at: datetime = Field(default_factory=_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=_now, alias="updatedAt")
    message_count: int = Field(default=0, alias="messageCount")
    doc_type: str = Field(default=DOC_TYPE_CONVERSATION, alias="docType")


class Message(BaseModel):
    """A single turn within a conversation."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=_new_id)
    user_id: str = Field(alias="userId")
    conversation_id: str = Field(alias="conversationId")
    role: Role
    content: str
    created_at: datetime = Field(default_factory=_now, alias="createdAt")
    # Foundry response id — lets a stored turn be correlated back to a trace.
    response_id: str | None = Field(default=None, alias="responseId")
    doc_type: str = Field(default=DOC_TYPE_MESSAGE, alias="docType")


# --------------------------------------------------------------------------- #
# Request / response payloads
# --------------------------------------------------------------------------- #


class CreateConversationRequest(BaseModel):
    """Body for creating a conversation."""

    agent_name: Annotated[str, Field(alias="agentName", min_length=1, max_length=63)]
    title: Annotated[str, Field(min_length=1, max_length=200)] = "New conversation"

    model_config = ConfigDict(populate_by_name=True)


class SendMessageRequest(BaseModel):
    """Body for posting a user turn.

    `max_length` is a first line of defence; the service also truncates against the
    configured ceiling so the limit is enforced even if this model is bypassed.
    """

    content: Annotated[str, Field(min_length=1, max_length=32_000)]


class MessageResponse(BaseModel):
    """A message as returned to the client."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    conversation_id: str = Field(serialization_alias="conversationId")
    role: Role
    content: str
    created_at: datetime = Field(serialization_alias="createdAt")

    @classmethod
    def from_domain(cls, message: Message) -> MessageResponse:
        return cls(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
        )


class ConversationResponse(BaseModel):
    """A conversation as returned to the client.

    Deliberately omits `foundry_conversation_id` — an internal platform handle that
    clients have no need for.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    agent_name: str = Field(serialization_alias="agentName")
    title: str
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")
    message_count: int = Field(serialization_alias="messageCount")

    @classmethod
    def from_domain(cls, conversation: Conversation) -> ConversationResponse:
        return cls(
            id=conversation.id,
            agent_name=conversation.agent_name,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=conversation.message_count,
        )


class SendMessageResponse(BaseModel):
    """The persisted user turn plus the agent's reply."""

    model_config = ConfigDict(populate_by_name=True)

    user_message: MessageResponse = Field(serialization_alias="userMessage")
    assistant_message: MessageResponse = Field(serialization_alias="assistantMessage")


class AgentSummary(BaseModel):
    """An agent the API is configured to talk to."""

    name: str


class HealthResponse(BaseModel):
    """Liveness / readiness payload."""

    status: str
    detail: dict[str, Any] = Field(default_factory=dict)
