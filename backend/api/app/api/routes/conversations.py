"""Conversation and message endpoints.

Every route derives the user from the validated `Principal`. No route accepts a
user id from the client — that is what makes cross-user access impossible rather
than merely discouraged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from app.dependencies import ConversationServiceDep
from app.domain.models import (
    ConversationResponse,
    CreateConversationRequest,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.security.auth import Principal, get_principal

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

PrincipalDep = Annotated[Principal, Depends(get_principal)]
ConversationId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest,
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> ConversationResponse:
    """Start a new conversation with one of the configured agents."""
    conversation = await service.create_conversation(
        user_id=principal.user_id,
        agent_name=body.agent_name,
        title=body.title,
    )
    return ConversationResponse.from_domain(conversation)


@router.get("")
async def list_conversations(
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> list[ConversationResponse]:
    """List the caller's conversations, most recently updated first."""
    conversations = await service.list_conversations(principal.user_id)
    return [ConversationResponse.from_domain(c) for c in conversations]


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: ConversationId,
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> ConversationResponse:
    """Fetch a single conversation owned by the caller."""
    conversation = await service.get_conversation(principal.user_id, conversation_id)
    return ConversationResponse.from_domain(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: ConversationId,
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> None:
    """Delete a conversation and all of its messages."""
    await service.delete_conversation(principal.user_id, conversation_id)


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: ConversationId,
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> list[MessageResponse]:
    """Return the full transcript, oldest turn first."""
    messages = await service.list_messages(principal.user_id, conversation_id)
    return [MessageResponse.from_domain(m) for m in messages]


@router.post("/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    conversation_id: ConversationId,
    body: SendMessageRequest,
    principal: PrincipalDep,
    service: ConversationServiceDep,
) -> SendMessageResponse:
    """Send a turn to the agent and return both the user turn and the reply."""
    user_message, assistant_message = await service.send_message(
        user_id=principal.user_id,
        conversation_id=conversation_id,
        content=body.content,
    )
    return SendMessageResponse(
        user_message=MessageResponse.from_domain(user_message),
        assistant_message=MessageResponse.from_domain(assistant_message),
    )
