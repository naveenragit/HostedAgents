"""Conversation orchestration across Foundry- and Cosmos-owned transcripts.

The API owns user scoping and the link from an application conversation to its
storage strategy. Research transcripts live in Foundry Conversations; triage
transcripts use the custom Cosmos implementation. Hosted agents never receive
database credentials.
"""

from __future__ import annotations

import hashlib
import logging

from app.domain.models import Conversation, ConversationManager, Message, Role
from app.repositories.mongo import (
    ConversationRepository,
    Page,
    RepositoryActiveTurnError,
    RepositoryConversationBusyError,
    RepositoryConversationDeletingError,
    RepositoryConversationNotFoundError,
    RepositoryInvariantError,
    RepositoryTurnCompletionRejectedError,
    RepositoryTurnConflictError,
    RepositoryTurnIndeterminateError,
    RepositoryTurnInvocationRejectedError,
    RepositoryTurnPendingError,
)
from app.services.agent_client import (
    AgentClient,
    AgentConversationMessage,
    AgentInputItem,
)

_LOGGER = logging.getLogger("api.conversations")

_TITLE_MAX_CHARS = 60


class ConversationNotFoundError(LookupError):
    """Raised when a conversation does not exist for the requesting user."""


class ConversationConflictError(RuntimeError):
    """Raised when deletion or another in-flight turn fences an operation."""


class DeletionPendingError(ConversationConflictError):
    """Raised after tombstoning while an active turn is still draining."""


class IdempotencyConflictError(ConversationConflictError):
    """Raised when an idempotency key is reused for different content."""


class TurnPendingError(ConversationConflictError):
    """Raised when the same idempotent request is already running."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Turn is already pending.")
        self.retry_after_seconds = retry_after_seconds


class TurnOutcomeUnknownError(ConversationConflictError):
    """Raised when a hosted-agent dispatch cannot be safely repeated."""


class InputTooLongError(ValueError):
    """Raised when input exceeds the configured server-side limit."""

    def __init__(self, max_chars: int) -> None:
        super().__init__(f"Message content must not exceed {max_chars} characters.")
        self.max_chars = max_chars


class AgentOutputTooLongError(RuntimeError):
    """Raised when model output exceeds the configured persistence limit."""


class ConversationService:
    """Coordinates the agent client and the repository for one request."""

    def __init__(
        self,
        repository: ConversationRepository,
        agent_client: AgentClient,
        max_input_chars: int,
        max_output_chars: int,
        history_max_chars: int,
        history_max_messages: int,
        turn_lease_seconds: int,
        foundry_conversation_agents: tuple[str, ...] = (),
    ) -> None:
        self._repository = repository
        self._agents = agent_client
        self._max_input_chars = max_input_chars
        self._max_output_chars = max_output_chars
        self._history_max_chars = history_max_chars
        self._history_max_messages = history_max_messages
        self._turn_lease_seconds = turn_lease_seconds
        self._foundry_conversation_agents = frozenset(foundry_conversation_agents)

    async def create_conversation(
        self,
        user_id: str,
        agent_name: str,
        title: str,
    ) -> Conversation:
        """Create a conversation and its strategy-specific durable state."""
        self._agents.ensure_known(agent_name)

        conversation_manager = (
            ConversationManager.FOUNDRY
            if agent_name in self._foundry_conversation_agents
            else ConversationManager.COSMOS
        )
        foundry_conversation_id = None
        if conversation_manager is ConversationManager.FOUNDRY:
            foundry_conversation_id = await self._agents.create_conversation(agent_name)

        conversation = Conversation(
            user_id=user_id,
            agent_name=agent_name,
            title=title.strip()[:_TITLE_MAX_CHARS] or "New conversation",
            conversation_manager=conversation_manager,
            foundry_conversation_id=foundry_conversation_id,
        )

        try:
            await self._repository.create_conversation(conversation)
        except Exception:
            if foundry_conversation_id is not None:
                try:
                    await self._agents.delete_conversation(
                        agent_name,
                        foundry_conversation_id,
                    )
                except Exception as cleanup_error:  # noqa: BLE001 - preserve create failure
                    _LOGGER.error(
                        "orphaned Foundry conversation cleanup failed",
                        extra={
                            "conversation_id": conversation.id,
                            "agent": agent_name,
                            "error_type": type(cleanup_error).__name__,
                        },
                    )
            raise
        _LOGGER.info(
            "conversation created",
            extra={"conversation_id": conversation.id, "agent": agent_name},
        )
        return conversation

    async def list_conversations(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[Conversation]:
        return await self._repository.list_conversations(user_id, limit=limit, cursor=cursor)

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation:
        conversation = await self._repository.get_conversation(user_id, conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def list_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[Message]:
        # Resolve first so someone else's id returns 404 rather than an empty list.
        conversation = await self.get_conversation(user_id, conversation_id)
        if conversation.conversation_manager is ConversationManager.FOUNDRY:
            foundry_id = _require_foundry_conversation_id(conversation)
            page = await self._agents.list_conversation_messages(
                conversation.agent_name,
                foundry_id,
                limit=limit,
                cursor=cursor,
            )
            return Page(
                items=[
                    _foundry_message_to_domain(user_id, conversation_id, message)
                    for message in page.items
                ],
                next_cursor=page.next_cursor,
            )
        return await self._repository.list_messages(
            user_id,
            conversation_id,
            limit=limit,
            cursor=cursor,
        )

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        conversation = await self._repository.get_conversation_for_deletion(
            user_id,
            conversation_id,
        )
        if conversation is None:
            if await self._repository.delete_conversation(user_id, conversation_id):
                return
            raise ConversationNotFoundError(conversation_id)

        try:
            if conversation.conversation_manager is ConversationManager.FOUNDRY:
                await self._repository.delete_conversation(
                    user_id,
                    conversation_id,
                    purge=False,
                )
                await self._agents.delete_conversation(
                    conversation.agent_name,
                    _require_foundry_conversation_id(conversation),
                )
            deleted = await self._repository.delete_conversation(user_id, conversation_id)
        except RepositoryActiveTurnError as exc:
            raise DeletionPendingError(conversation_id) from exc
        if not deleted:
            raise ConversationNotFoundError(conversation_id)

    async def send_message(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        idempotency_key: str,
    ) -> tuple[Message, Message]:
        """Run an idempotent turn using the conversation's transcript owner."""
        conversation = await self.get_conversation(user_id, conversation_id)
        normalized = self._validate_content(content)
        content_hash = hashlib.sha256(normalized.encode()).hexdigest()

        try:
            reservation = await self._repository.reserve_turn(
                user_id,
                conversation_id,
                idempotency_key,
                content_hash,
                lease_seconds=self._turn_lease_seconds,
            )
        except RepositoryConversationNotFoundError as exc:
            raise ConversationNotFoundError(conversation_id) from exc
        except RepositoryConversationDeletingError as exc:
            raise ConversationConflictError("Conversation is being deleted.") from exc
        except RepositoryConversationBusyError as exc:
            raise ConversationConflictError("Another turn is already running.") from exc
        except RepositoryTurnConflictError as exc:
            raise IdempotencyConflictError(idempotency_key) from exc
        except RepositoryTurnPendingError as exc:
            raise TurnPendingError(exc.retry_after_seconds) from exc
        except RepositoryTurnIndeterminateError as exc:
            raise TurnOutcomeUnknownError(
                "The prior agent request has an unknown outcome and will not be repeated."
            ) from exc

        if reservation.is_replay:
            if conversation.conversation_manager is ConversationManager.FOUNDRY:
                foundry_id = _require_foundry_conversation_id(conversation)
                user_message_id = reservation.turn.user_message_id
                assistant_message_id = reservation.turn.assistant_message_id
                if user_message_id is None or assistant_message_id is None:
                    raise RepositoryInvariantError(
                        "Completed Foundry turn replay is missing item references."
                    )
                foundry_user, foundry_assistant = await self._agents.get_conversation_messages(
                    conversation.agent_name,
                    foundry_id,
                    user_message_id,
                    assistant_message_id,
                )
                return (
                    _foundry_message_to_domain(user_id, conversation_id, foundry_user),
                    _foundry_message_to_domain(user_id, conversation_id, foundry_assistant),
                )
            user_message = reservation.user_message
            assistant_message = reservation.assistant_message
            if user_message is None or assistant_message is None:
                raise RepositoryInvariantError("Completed turn replay is missing messages.")
            return user_message, assistant_message

        turn = reservation.turn

        try:
            if conversation.conversation_manager is ConversationManager.FOUNDRY:
                agent_input: list[AgentInputItem] = [{"role": "user", "content": normalized}]
            else:
                history = await self._repository.list_recent_messages(
                    user_id,
                    conversation_id,
                    limit=self._history_max_messages,
                )
                agent_input = build_agent_input(
                    history,
                    normalized,
                    max_history_chars=self._history_max_chars,
                    max_total_chars=self._max_input_chars,
                )
            await self._repository.begin_turn_invocation(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
            )
        except RepositoryTurnInvocationRejectedError as exc:
            await self._mark_failed(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                "invocation_fenced",
            )
            raise ConversationConflictError(
                "Turn invocation was superseded or the conversation is being deleted."
            ) from exc
        except Exception as exc:
            await self._mark_failed(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                type(exc).__name__,
            )
            raise

        try:
            if conversation.conversation_manager is ConversationManager.FOUNDRY:
                reply = await self._agents.send(
                    agent_name=conversation.agent_name,
                    input_items=agent_input,
                    conversation_id=_require_foundry_conversation_id(conversation),
                )
            else:
                reply = await self._agents.send(
                    agent_name=conversation.agent_name,
                    input_items=agent_input,
                )

            assistant_content = reply.text.strip()
            if len(assistant_content) > self._max_output_chars:
                raise AgentOutputTooLongError(
                    f"Agent output exceeded {self._max_output_chars} characters."
                )

            if conversation.conversation_manager is ConversationManager.FOUNDRY:
                if reply.user_message_id is None or reply.assistant_message_id is None:
                    raise RepositoryInvariantError(
                        "Foundry turn completed without durable item references."
                    )
                user_message = Message(
                    id=reply.user_message_id,
                    user_id=user_id,
                    conversation_id=conversation.id,
                    role=Role.USER,
                    content=normalized,
                    created_at=None,
                )
                assistant_message = Message(
                    id=reply.assistant_message_id,
                    user_id=user_id,
                    conversation_id=conversation.id,
                    role=Role.ASSISTANT,
                    content=assistant_content,
                    created_at=None,
                    response_id=reply.response_id,
                )
            else:
                user_message = Message(
                    user_id=user_id,
                    conversation_id=conversation.id,
                    role=Role.USER,
                    content=normalized,
                )
                assistant_message = Message(
                    user_id=user_id,
                    conversation_id=conversation.id,
                    role=Role.ASSISTANT,
                    content=assistant_content,
                    response_id=reply.response_id,
                )

            await self._repository.complete_turn(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                user_message,
                assistant_message,
                title=(
                    _derive_title(normalized)
                    if conversation.conversation_manager is ConversationManager.COSMOS
                    else None
                ),
                persist_messages=(conversation.conversation_manager is ConversationManager.COSMOS),
            )
        except RepositoryTurnCompletionRejectedError as exc:
            await self._mark_indeterminate(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                "completion_fenced",
            )
            raise ConversationConflictError(
                "Turn completion was superseded or the conversation is being deleted."
            ) from exc
        except AgentOutputTooLongError:
            await self._mark_indeterminate(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                "output_too_long",
            )
            raise
        except Exception as exc:
            await self._mark_indeterminate(
                user_id,
                conversation_id,
                turn.id,
                turn.attempt_id,
                type(exc).__name__,
            )
            raise TurnOutcomeUnknownError(
                "The agent request outcome could not be confirmed and will not be repeated."
            ) from exc

        _LOGGER.info(
            "turn completed",
            extra={
                "conversation_id": conversation.id,
                "agent": conversation.agent_name,
                "input_chars": len(normalized),
                "output_chars": len(assistant_content),
            },
        )
        return user_message, assistant_message

    def _validate_content(self, content: str) -> str:
        """Normalize surrounding whitespace and enforce the configured hard limit."""
        stripped = content.strip()
        if not stripped:
            raise ValueError("Message content must not be blank.")
        if len(stripped) > self._max_input_chars:
            raise InputTooLongError(self._max_input_chars)
        return stripped

    async def _mark_failed(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        failure_code: str,
    ) -> None:
        """Best-effort failure transition; an unrecorded attempt expires by lease."""
        try:
            await self._repository.fail_turn(
                user_id,
                conversation_id,
                turn_id,
                attempt_id,
                failure_code,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original request failure
            _LOGGER.error(
                "turn failure transition failed",
                extra={
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def _mark_indeterminate(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        failure_code: str,
    ) -> None:
        """Best-effort terminal transition after the remote dispatch boundary."""
        try:
            await self._repository.mark_turn_indeterminate(
                user_id,
                conversation_id,
                turn_id,
                attempt_id,
                failure_code,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original request failure
            _LOGGER.error(
                "turn indeterminate transition failed",
                extra={
                    "conversation_id": conversation_id,
                    "error_type": type(exc).__name__,
                },
            )


def build_agent_input(
    history: list[Message],
    new_user_content: str,
    *,
    max_history_chars: int,
    max_total_chars: int,
) -> list[AgentInputItem]:
    """Select the newest complete user/assistant pairs under a character budget.

    This is deliberately a character approximation, not an exact model-token
    calculation. An incomplete boundary pair is omitted rather than changing
    role order or sending an orphaned message.
    """
    history_budget = min(max_history_chars, max(0, max_total_chars - len(new_user_content)))
    selected_pairs_newest_first: list[tuple[Message, Message]] = []
    used_chars = 0
    pair_end = len(history)
    if pair_end > 0 and history[pair_end - 1].role is Role.USER:
        pair_end -= 1

    while pair_end >= 2:
        user_message = history[pair_end - 2]
        assistant_message = history[pair_end - 1]
        if user_message.role is not Role.USER or assistant_message.role is not Role.ASSISTANT:
            break

        pair_chars = len(user_message.content) + len(assistant_message.content)
        if used_chars + pair_chars > history_budget:
            break
        selected_pairs_newest_first.append((user_message, assistant_message))
        used_chars += pair_chars
        pair_end -= 2

    items: list[AgentInputItem] = []
    for user_message, assistant_message in reversed(selected_pairs_newest_first):
        items.extend(
            (
                {"role": "user", "content": user_message.content},
                {"role": "assistant", "content": assistant_message.content},
            )
        )
    items.append({"role": "user", "content": new_user_content})
    return items


def _derive_title(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _TITLE_MAX_CHARS:
        return collapsed or "New conversation"
    return collapsed[: _TITLE_MAX_CHARS - 1].rstrip() + "\u2026"


def _require_foundry_conversation_id(conversation: Conversation) -> str:
    conversation_id = conversation.foundry_conversation_id
    if conversation_id is None or not conversation_id:
        raise RepositoryInvariantError("Foundry-managed conversation is missing its Foundry id.")
    return conversation_id


def _foundry_message_to_domain(
    user_id: str,
    conversation_id: str,
    message: AgentConversationMessage,
) -> Message:
    return Message(
        id=message.id,
        user_id=user_id,
        conversation_id=conversation_id,
        role=Role(message.role),
        content=message.content,
        created_at=None,
    )
