"""Offline tests for transactional turn, deletion, and pagination behavior."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.domain.models import Conversation, ConversationManager, Message, Role, TurnStatus
from app.repositories.mongo import (
    ConversationRepository,
    RepositoryActiveTurnError,
    RepositoryTurnCompletionRejectedError,
    RepositoryTurnConflictError,
    RepositoryTurnIndeterminateError,
    RepositoryTurnPendingError,
    _to_document,
)

USER = "user-alpha"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000001"
KEY = "00000000-0000-0000-0000-000000000002"
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_MISSING = object()


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue

        actual = document.get(key, _MISSING)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in":
                    normalized = None if actual is _MISSING else actual
                    if normalized not in operand:
                        return False
                elif operator == "$ne":
                    if actual is not _MISSING and actual == operand:
                        return False
                elif operator == "$lt":
                    if actual is _MISSING or actual >= operand:
                        return False
                elif operator == "$gt":
                    if actual is _MISSING or actual <= operand:
                        return False
                else:  # pragma: no cover - test fake rejects unsupported production queries
                    raise AssertionError(f"Unsupported operator: {operator}")
        elif actual is _MISSING:
            if expected is not None:
                return False
        elif actual != expected:
            return False
    return True


class MemoryCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    def sort(self, key_or_list: Any, direction: int | None = None) -> MemoryCursor:
        fields = key_or_list if isinstance(key_or_list, list) else [(key_or_list, direction)]
        for field, order in reversed(fields):
            self._documents.sort(
                key=lambda item: str(item.get(field, "")),
                reverse=order == -1,
            )
        return self

    def limit(self, count: int) -> MemoryCursor:
        self._documents = self._documents[:count]
        return self

    def __aiter__(self) -> Any:
        async def generate() -> Any:
            for document in self._documents:
                yield copy.deepcopy(document)

        return generate()


class MemoryCollection:
    def __init__(
        self,
        documents: list[dict[str, Any]] | None = None,
        *,
        fail_on_insert: int | None = None,
    ) -> None:
        self.documents = copy.deepcopy(documents or [])
        self._insert_count = 0
        self._fail_on_insert = fail_on_insert

    async def insert_one(self, document: dict[str, Any], **_kwargs: Any) -> Any:
        self._insert_count += 1
        if self._insert_count == self._fail_on_insert:
            raise RuntimeError("simulated insert failure")
        if any(item.get("_id") == document.get("_id") for item in self.documents):
            raise AssertionError("duplicate _id in test fake")
        self.documents.append(copy.deepcopy(document))
        return SimpleNamespace(inserted_id=document["_id"])

    async def find_one(
        self,
        query: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        match = next((item for item in self.documents if _matches(item, query)), None)
        return copy.deepcopy(match) if match is not None else None

    def find(self, query: dict[str, Any]) -> MemoryCursor:
        return MemoryCursor(
            [copy.deepcopy(item) for item in self.documents if _matches(item, query)]
        )

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        **_kwargs: Any,
    ) -> Any:
        for document in self.documents:
            if not _matches(document, query):
                continue
            for key, value in update.get("$set", {}).items():
                document[key] = value
            for key in update.get("$unset", {}):
                document.pop(key, None)
            for key, value in update.get("$inc", {}).items():
                document[key] = document.get(key, 0) + value
            return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def replace_one(
        self,
        query: dict[str, Any],
        replacement: dict[str, Any],
        **_kwargs: Any,
    ) -> Any:
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents[index] = copy.deepcopy(replacement)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query: dict[str, Any], **_kwargs: Any) -> Any:
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query: dict[str, Any], **_kwargs: Any) -> Any:
        retained = [document for document in self.documents if not _matches(document, query)]
        deleted_count = len(self.documents) - len(retained)
        self.documents = retained
        return SimpleNamespace(deleted_count=deleted_count)


class MemoryTransactions:
    """Snapshot rollback approximates the all-or-nothing behavior under test."""

    def __init__(self, *collections: MemoryCollection) -> None:
        self._collections = collections
        self.calls = 0

    async def run(self, operation: Any) -> Any:
        self.calls += 1
        snapshots = [copy.deepcopy(collection.documents) for collection in self._collections]
        try:
            return await operation(object())
        except BaseException:
            for collection, snapshot in zip(self._collections, snapshots, strict=True):
                collection.documents = snapshot
            raise


def _repository(
    *,
    conversations: MemoryCollection | None = None,
    messages: MemoryCollection | None = None,
    turns: MemoryCollection | None = None,
    clock: Any = None,
) -> tuple[ConversationRepository, MemoryCollection, MemoryCollection, MemoryCollection]:
    conversation_collection = conversations or MemoryCollection(
        [_to_document(Conversation(id=CONVERSATION_ID, user_id=USER, agent_name="research-agent"))]
    )
    message_collection = messages or MemoryCollection()
    turn_collection = turns or MemoryCollection()
    transactions = MemoryTransactions(
        conversation_collection,
        message_collection,
        turn_collection,
    )
    repository = ConversationRepository(
        cast(Any, conversation_collection),
        cast(Any, message_collection),
        cast(Any, turn_collection),
        transactions,
        clock=clock or (lambda: NOW),
        attempt_id_factory=iter(("attempt-1", "attempt-2", "attempt-3")).__next__,
        deletion_id_factory=lambda: "deletion-1",
        deletion_tombstone_retention_seconds=86_400,
    )
    return repository, conversation_collection, message_collection, turn_collection


def _message(message_id: str, role: Role, content: str) -> Message:
    return Message(
        id=message_id,
        user_id=USER,
        conversation_id=CONVERSATION_ID,
        role=role,
        content=content,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_completed_turn_is_replayed_without_a_new_attempt() -> None:
    repository, conversations, messages, turns = _repository()
    reservation = await repository.reserve_turn(
        USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60
    )
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
    )
    user_message = _message("user-message", Role.USER, "question")
    assistant_message = _message("assistant-message", Role.ASSISTANT, "answer")

    await repository.complete_turn(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
        user_message,
        assistant_message,
        title="question",
    )
    replay = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    assert replay.is_replay
    assert replay.user_message == user_message
    assert replay.assistant_message == assistant_message
    assert len(messages.documents) == 2
    assert conversations.documents[0]["messageCount"] == 2
    assert conversations.documents[0]["activeTurnId"] is None
    assert turns.documents[0]["status"] == TurnStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_foundry_completed_turn_stores_references_without_message_content() -> None:
    conversations = MemoryCollection(
        [
            _to_document(
                Conversation(
                    id=CONVERSATION_ID,
                    user_id=USER,
                    agent_name="research-agent",
                    conversation_manager=ConversationManager.FOUNDRY,
                    foundry_conversation_id="foundry-conversation-1",
                )
            )
        ]
    )
    repository, conversations, messages, turns = _repository(conversations=conversations)
    reservation = await repository.reserve_turn(
        USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60
    )
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
    )

    await repository.complete_turn(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
        _message("foundry-user", Role.USER, "question"),
        _message("foundry-assistant", Role.ASSISTANT, "answer"),
        title="question",
        persist_messages=False,
    )
    replay = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    assert replay.is_replay
    assert replay.user_message is None
    assert replay.assistant_message is None
    assert replay.turn.user_message_id == "foundry-user"
    assert replay.turn.assistant_message_id == "foundry-assistant"
    assert messages.documents == []
    assert conversations.documents[0]["foundryConversationId"] == "foundry-conversation-1"
    assert conversations.documents[0]["messageCount"] == 2
    assert turns.documents[0]["status"] == TurnStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_pending_duplicate_and_content_hash_conflict_are_rejected() -> None:
    repository, _conversations, _messages, _turns = _repository()
    await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    with pytest.raises(RepositoryTurnPendingError) as pending:
        await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    with pytest.raises(RepositoryTurnConflictError):
        await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "different", lease_seconds=60)

    assert pending.value.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_failed_turn_can_retry_with_a_new_fenced_attempt() -> None:
    repository, conversations, _messages, turns = _repository()
    first = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    assert await repository.fail_turn(
        USER,
        CONVERSATION_ID,
        first.turn.id,
        first.turn.attempt_id,
        "agent_failure",
    )

    second = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    assert second.turn.attempt_id == "attempt-2"
    assert turns.documents[0]["status"] == TurnStatus.PENDING.value
    assert conversations.documents[0]["activeTurnId"] == second.turn.id


@pytest.mark.asyncio
async def test_dispatched_turn_cannot_be_reinvoked_after_failure() -> None:
    repository, conversations, _messages, turns = _repository()
    first = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        first.turn.id,
        first.turn.attempt_id,
    )
    assert await repository.mark_turn_indeterminate(
        USER,
        CONVERSATION_ID,
        first.turn.id,
        first.turn.attempt_id,
        "timeout",
    )

    with pytest.raises(RepositoryTurnIndeterminateError):
        await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    assert turns.documents[0]["status"] == TurnStatus.INDETERMINATE.value
    assert conversations.documents[0]["activeTurnId"] is None


@pytest.mark.asyncio
async def test_expired_dispatched_turn_becomes_terminally_indeterminate() -> None:
    current = NOW
    repository, conversations, _messages, turns = _repository(clock=lambda: current)
    first = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        first.turn.id,
        first.turn.attempt_id,
    )
    current += timedelta(seconds=61)

    with pytest.raises(RepositoryTurnIndeterminateError):
        await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    assert turns.documents[0]["status"] == TurnStatus.INDETERMINATE.value
    assert conversations.documents[0]["activeTurnId"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("begin_invocation", "expired_status"),
    [(False, TurnStatus.FAILED), (True, TurnStatus.INDETERMINATE)],
)
async def test_new_turn_recovers_an_expired_conversation_fence(
    begin_invocation: bool,
    expired_status: TurnStatus,
) -> None:
    current = NOW
    repository, conversations, _messages, turns = _repository(clock=lambda: current)
    first = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    if begin_invocation:
        await repository.begin_turn_invocation(
            USER,
            CONVERSATION_ID,
            first.turn.id,
            first.turn.attempt_id,
        )
    current += timedelta(seconds=61)

    second = await repository.reserve_turn(
        USER,
        CONVERSATION_ID,
        f"{KEY}-new",
        "new-hash",
        lease_seconds=60,
    )

    assert turns.documents[0]["status"] == expired_status.value
    assert turns.documents[0]["failureCode"] == "lease_expired"
    assert conversations.documents[0]["activeTurnId"] == second.turn.id


@pytest.mark.asyncio
async def test_stale_attempt_cannot_complete_after_retry() -> None:
    repository, _conversations, messages, _turns = _repository()
    first = await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    await repository.fail_turn(
        USER,
        CONVERSATION_ID,
        first.turn.id,
        first.turn.attempt_id,
        "agent_failure",
    )
    await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)

    with pytest.raises(RepositoryTurnCompletionRejectedError):
        await repository.complete_turn(
            USER,
            CONVERSATION_ID,
            first.turn.id,
            first.turn.attempt_id,
            _message("stale-user", Role.USER, "question"),
            _message("stale-assistant", Role.ASSISTANT, "answer"),
            title=None,
        )

    assert messages.documents == []


@pytest.mark.asyncio
async def test_completion_rolls_back_if_second_message_insert_fails() -> None:
    messages = MemoryCollection(fail_on_insert=2)
    repository, conversations, messages, turns = _repository(messages=messages)
    reservation = await repository.reserve_turn(
        USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60
    )
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
    )

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        await repository.complete_turn(
            USER,
            CONVERSATION_ID,
            reservation.turn.id,
            reservation.turn.attempt_id,
            _message("user-message", Role.USER, "question"),
            _message("assistant-message", Role.ASSISTANT, "answer"),
            title="question",
        )

    assert messages.documents == []
    assert conversations.documents[0]["messageCount"] == 0
    assert conversations.documents[0]["activeTurnId"] == reservation.turn.id
    assert turns.documents[0]["status"] == TurnStatus.INVOKING.value


@pytest.mark.asyncio
async def test_deletion_tombstone_rejects_inflight_completion_then_purges() -> None:
    repository, conversations, messages, turns = _repository()
    reservation = await repository.reserve_turn(
        USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60
    )
    await repository.begin_turn_invocation(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
    )

    with pytest.raises(RepositoryActiveTurnError):
        await repository.delete_conversation(USER, CONVERSATION_ID)
    assert conversations.documents[0]["state"] == "deleting"

    with pytest.raises(RepositoryTurnCompletionRejectedError):
        await repository.complete_turn(
            USER,
            CONVERSATION_ID,
            reservation.turn.id,
            reservation.turn.attempt_id,
            _message("user-message", Role.USER, "question"),
            _message("assistant-message", Role.ASSISTANT, "answer"),
            title=None,
        )
    await repository.mark_turn_indeterminate(
        USER,
        CONVERSATION_ID,
        reservation.turn.id,
        reservation.turn.attempt_id,
        "deletion",
    )

    assert conversations.documents == [
        {
            "_id": CONVERSATION_ID,
            "userId": USER,
            "state": "deleting",
            "deletionRequestId": "deletion-1",
            "deletionRequestedAt": "2026-08-06T12:00:00.000000Z",
            "deletedAt": "2026-08-06T12:00:00.000000Z",
            "expiresAt": NOW + timedelta(days=1),
            "docType": "conversationTombstone",
        }
    ]
    assert messages.documents == []
    assert turns.documents == []

    original_tombstone = copy.deepcopy(conversations.documents[0])
    assert await repository.delete_conversation(USER, CONVERSATION_ID)
    assert conversations.documents[0] == original_tombstone


@pytest.mark.asyncio
async def test_expired_pending_turn_does_not_block_deletion_forever() -> None:
    current = NOW
    repository, conversations, _messages, _turns = _repository(clock=lambda: current)
    await repository.reserve_turn(USER, CONVERSATION_ID, KEY, "hash", lease_seconds=60)
    current += timedelta(seconds=61)

    assert await repository.delete_conversation(USER, CONVERSATION_ID)
    assert conversations.documents[0]["docType"] == "conversationTombstone"


@pytest.mark.asyncio
async def test_conversation_cursor_uses_id_as_stable_tiebreak() -> None:
    conversations = MemoryCollection(
        [
            _to_document(
                Conversation(
                    id=f"00000000-0000-0000-0000-00000000000{suffix}",
                    user_id=USER,
                    agent_name="research-agent",
                    updated_at=NOW,
                )
            )
            for suffix in (1, 2, 3)
        ]
    )
    repository, _conversations, _messages, _turns = _repository(conversations=conversations)

    first = await repository.list_conversations(USER, limit=2)
    second = await repository.list_conversations(USER, limit=2, cursor=first.next_cursor)

    assert [item.id[-1] for item in first.items] == ["3", "2"]
    assert first.next_cursor is not None
    assert [item.id[-1] for item in second.items] == ["1"]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_message_pagination_and_recent_window_preserve_chronology() -> None:
    messages = MemoryCollection(
        [
            _to_document(
                Message(
                    id=f"message-{index}",
                    user_id=USER,
                    conversation_id=CONVERSATION_ID,
                    role=Role.USER if index % 2 else Role.ASSISTANT,
                    content=str(index),
                    created_at=NOW + timedelta(seconds=index),
                )
            )
            for index in range(1, 5)
        ]
    )
    repository, _conversations, _messages, _turns = _repository(messages=messages)

    first = await repository.list_messages(USER, CONVERSATION_ID, limit=2)
    second = await repository.list_messages(
        USER,
        CONVERSATION_ID,
        limit=2,
        cursor=first.next_cursor,
    )
    recent = await repository.list_recent_messages(USER, CONVERSATION_ID, limit=2)

    assert [item.content for item in first.items] == ["3", "4"]
    assert [item.content for item in second.items] == ["1", "2"]
    assert [item.content for item in recent] == ["3", "4"]


@pytest.mark.asyncio
async def test_completed_turns_have_strictly_increasing_message_timestamps() -> None:
    repository, _conversations, _messages, _turns = _repository(clock=lambda: NOW)

    for index in (1, 2):
        reservation = await repository.reserve_turn(
            USER,
            CONVERSATION_ID,
            f"{KEY}-{index}",
            f"hash-{index}",
            lease_seconds=60,
        )
        await repository.begin_turn_invocation(
            USER,
            CONVERSATION_ID,
            reservation.turn.id,
            reservation.turn.attempt_id,
        )
        await repository.complete_turn(
            USER,
            CONVERSATION_ID,
            reservation.turn.id,
            reservation.turn.attempt_id,
            _message(f"user-{index}", Role.USER, f"question-{index}"),
            _message(f"assistant-{index}", Role.ASSISTANT, f"answer-{index}"),
            title=None,
        )

    page = await repository.list_messages(USER, CONVERSATION_ID, limit=10)
    recent = await repository.list_recent_messages(USER, CONVERSATION_ID, limit=10)

    assert [item.content for item in page.items] == [
        "question-1",
        "answer-1",
        "question-2",
        "answer-2",
    ]
    assert [item.content for item in recent] == [item.content for item in page.items]
    timestamps = [item.created_at for item in page.items]
    assert all(timestamp is not None for timestamp in timestamps)
    non_null_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    assert all(earlier < later for earlier, later in pairwise(non_null_timestamps))
