"""Domain models.

These are the API contract and the Cosmos document shape. Keeping them in one
place means a field can't drift between the wire format and storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_TYPE_CONVERSATION = "conversation"
DOC_TYPE_MESSAGE = "message"
DOC_TYPE_TURN = "turn"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


class Role(str, Enum):
    """Author of a message."""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationState(str, Enum):
    """Lifecycle state used to fence sends from deletion."""

    ACTIVE = "active"
    DELETING = "deleting"


class ConversationManager(str, Enum):
    """System that owns the durable transcript for a conversation."""

    COSMOS = "cosmos"
    FOUNDRY = "foundry"


class TurnStatus(str, Enum):
    """Durable idempotency state for one user request."""

    PENDING = "pending"
    INVOKING = "invoking"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


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
    created_at: datetime = Field(default_factory=_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=_now, alias="updatedAt")
    message_count: int = Field(default=0, alias="messageCount")
    state: ConversationState = ConversationState.ACTIVE
    active_turn_id: str | None = Field(default=None, alias="activeTurnId")
    conversation_manager: ConversationManager = Field(
        default=ConversationManager.COSMOS,
        alias="conversationManager",
    )
    foundry_conversation_id: str | None = Field(default=None, alias="foundryConversationId")
    doc_type: str = Field(default=DOC_TYPE_CONVERSATION, alias="docType")


class Message(BaseModel):
    """A single turn within a conversation."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=_new_id)
    user_id: str = Field(alias="userId")
    conversation_id: str = Field(alias="conversationId")
    role: Role
    content: str
    # Foundry conversation items do not expose per-item timestamps. Those items
    # are projected with None; Cosmos-managed messages always have a timestamp.
    created_at: datetime | None = Field(default_factory=_now, alias="createdAt")
    # Foundry response id — lets a stored turn be correlated back to a trace.
    response_id: str | None = Field(default=None, alias="responseId")
    doc_type: str = Field(default=DOC_TYPE_MESSAGE, alias="docType")


class Turn(BaseModel):
    """Durable state for an idempotent send attempt."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    user_id: str = Field(alias="userId")
    conversation_id: str = Field(alias="conversationId")
    idempotency_key: str = Field(alias="idempotencyKey")
    content_hash: str = Field(alias="contentHash")
    status: TurnStatus
    attempt_id: str = Field(alias="attemptId")
    lease_expires_at: datetime | None = Field(default=None, alias="leaseExpiresAt")
    user_message_id: str | None = Field(default=None, alias="userMessageId")
    assistant_message_id: str | None = Field(default=None, alias="assistantMessageId")
    failure_code: str | None = Field(default=None, alias="failureCode")
    created_at: datetime = Field(default_factory=_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=_now, alias="updatedAt")
    doc_type: str = Field(default=DOC_TYPE_TURN, alias="docType")


# --------------------------------------------------------------------------- #
# Request / response payloads
# --------------------------------------------------------------------------- #


class CreateConversationRequest(BaseModel):
    """Body for creating a conversation."""

    agent_name: Annotated[str, Field(alias="agentName", min_length=1, max_length=63)]
    title: Annotated[str, Field(min_length=1, max_length=200)] = "New conversation"

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("agent_name", "title")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SendMessageRequest(BaseModel):
    """Body for posting a user turn."""

    content: Annotated[str, Field(min_length=1, max_length=32_000)]

    model_config = ConfigDict(extra="forbid")

    @field_validator("content")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class MessageResponse(BaseModel):
    """A message as returned to the client."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    conversation_id: str = Field(serialization_alias="conversationId")
    role: Role
    content: str
    created_at: datetime | None = Field(serialization_alias="createdAt")

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
    """A conversation as returned to the client."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    agent_name: str = Field(serialization_alias="agentName")
    title: str
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")
    message_count: int = Field(serialization_alias="messageCount")
    conversation_manager: ConversationManager = Field(serialization_alias="conversationManager")

    @classmethod
    def from_domain(cls, conversation: Conversation) -> ConversationResponse:
        return cls(
            id=conversation.id,
            agent_name=conversation.agent_name,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=conversation.message_count,
            conversation_manager=conversation.conversation_manager,
        )


class SendMessageResponse(BaseModel):
    """The persisted user turn plus the agent's reply."""

    model_config = ConfigDict(populate_by_name=True)

    conversation: ConversationResponse
    user_message: MessageResponse = Field(serialization_alias="userMessage")
    assistant_message: MessageResponse = Field(serialization_alias="assistantMessage")


class ConversationPageResponse(BaseModel):
    """One stable cursor page of conversations."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[ConversationResponse]
    next_cursor: str | None = Field(default=None, serialization_alias="nextCursor")


class MessagePageResponse(BaseModel):
    """One stable cursor page of transcript messages."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[MessageResponse]
    next_cursor: str | None = Field(default=None, serialization_alias="nextCursor")


class AgentSummary(BaseModel):
    """An agent the API is configured to talk to."""

    name: str


class CapabilitiesResponse(BaseModel):
    """Server-enforced limits needed by the browser client."""

    model_config = ConfigDict(populate_by_name=True)

    max_input_chars: int = Field(serialization_alias="maxInputChars")


class HealthResponse(BaseModel):
    """Liveness / readiness payload."""

    status: str
    detail: dict[str, Any] = Field(default_factory=dict)
