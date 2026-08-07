"""Conversation and message endpoints.

Every route derives the user from the validated `Principal`. No route accepts a
user id from the client — that is what makes cross-user access impossible rather
than merely discouraged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, status

from app.dependencies import ConversationServiceDep
from app.domain.models import (
    ConversationPageResponse,
    ConversationResponse,
    CreateConversationRequest,
    MessagePageResponse,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.security.auth import Principal, get_principal

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

PrincipalDep = Annotated[Principal, Depends(get_principal)]
ConversationId = Annotated[str, Path(min_length=1, max_length=64)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


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
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> ConversationPageResponse:
    """List the caller's conversations, most recently updated first."""
    page = await service.list_conversations(principal.user_id, limit=limit, cursor=cursor)
    return ConversationPageResponse(
        items=[ConversationResponse.from_domain(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


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
    limit: PageLimit = 100,
    cursor: Cursor = None,
) -> MessagePageResponse:
    """Return one stable transcript page in chronological order."""
    page = await service.list_messages(
        principal.user_id,
        conversation_id,
        limit=limit,
        cursor=cursor,
    )
    return MessagePageResponse(
        items=[MessageResponse.from_domain(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    conversation_id: ConversationId,
    body: SendMessageRequest,
    principal: PrincipalDep,
    service: ConversationServiceDep,
    idempotency_key: IdempotencyKey,
) -> SendMessageResponse:
    """Send a turn to the agent and return both the user turn and the reply."""
    user_message, assistant_message = await service.send_message(
        user_id=principal.user_id,
        conversation_id=conversation_id,
        content=body.content,
        idempotency_key=idempotency_key,
    )
    conversation = await service.get_conversation(principal.user_id, conversation_id)
    return SendMessageResponse(
        conversation=ConversationResponse.from_domain(conversation),
        user_message=MessageResponse.from_domain(user_message),
        assistant_message=MessageResponse.from_domain(assistant_message),
    )
