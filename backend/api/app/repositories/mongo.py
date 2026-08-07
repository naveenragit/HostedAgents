"""Persistence for conversations and messages on Azure Cosmos DB for MongoDB (vCore).

Why MongoDB and not the Cosmos NoSQL SDK
----------------------------------------
The target cluster is `*.mongocluster.cosmos.azure.com` — a Cosmos DB for MongoDB
vCore cluster. It speaks the MongoDB wire protocol, so `azure-cosmos` (the NoSQL
SDK) cannot talk to it. We use PyMongo's native async client instead.

Per-user isolation
------------------
Cosmos NoSQL enforces isolation with a partition key. vCore has no equivalent that
the driver applies automatically, so isolation here is structural in a different
way: **every** method takes `user_id` and every filter is built by `_scope()`,
which always injects `userId`. No query in this module can be issued without it,
and a compound index makes those lookups efficient.

Authentication
--------------
Keyless, via Microsoft Entra ID (`MONGODB-OIDC`) using `DefaultAzureCredential`.
No account key or password is read, stored, or logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Generic, Protocol, TypeVar, cast
from uuid import uuid4

from azure.core.credentials import TokenCredential
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.auth_oidc import OIDCCallback, OIDCCallbackContext, OIDCCallbackResult
from pymongo.errors import DuplicateKeyError

from app.domain.models import (
    Conversation,
    ConversationManager,
    ConversationState,
    Message,
    Turn,
    TurnStatus,
)

_LOGGER = logging.getLogger("api.repository")

_MAX_PAGE_SIZE: Final[int] = 200
_CURSOR_VERSION: Final[int] = 1
_DEFAULT_DELETION_TOMBSTONE_RETENTION_SECONDS: Final[int] = 86_400
_TOMBSTONE_DOC_TYPE: Final[str] = "conversationTombstone"

# Entra ID scope for Cosmos DB for MongoDB vCore / Azure DocumentDB data-plane access.
# Not a credential — it is the well-known audience URI the token is requested for.
_TOKEN_SCOPE: Final[str] = "https://ossrdbms-aad.database.windows.net/.default"  # noqa: S105

# NOTE: do NOT pass ALLOWED_HOSTS here. PyMongo enforces its host allow-list only
# for the interactive OIDC_HUMAN_CALLBACK flow, and raises
#   ConfigurationError: ALLOWED_HOSTS is only valid with OIDC_HUMAN_CALLBACK
# if it is supplied alongside the machine OIDC_CALLBACK we use. Docs and samples
# that show `ALLOWED_HOSTS: ['*.azure.com']` are describing the browser/driver
# flows (Node, Compass), not the Python machine callback.


class AzureIdentityTokenCallback(OIDCCallback):
    """Supplies a Microsoft Entra ID access token to the MongoDB OIDC handshake."""

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential

    def fetch(self, context: OIDCCallbackContext) -> OIDCCallbackResult:
        # `get_token` is blocking, but `DefaultAzureCredential` caches tokens in
        # memory and the driver only invokes this on connection setup and refresh,
        # so it is not on the per-request path.
        token = self._credential.get_token(_TOKEN_SCOPE)
        return OIDCCallbackResult(access_token=token.token)


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor is malformed or unsupported."""


class RepositoryConversationNotFoundError(LookupError):
    """Raised when a scoped conversation does not exist."""


class RepositoryConversationDeletingError(RuntimeError):
    """Raised when a turn is attempted after deletion has started."""


class RepositoryConversationBusyError(RuntimeError):
    """Raised when another turn currently owns the conversation fence."""


class RepositoryTurnConflictError(RuntimeError):
    """Raised when an idempotency key is reused with different content."""


class RepositoryTurnPendingError(RuntimeError):
    """Raised when the same idempotent request is still in progress."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Turn is still pending.")
        self.retry_after_seconds = retry_after_seconds


class RepositoryTurnIndeterminateError(RuntimeError):
    """Raised when a prior dispatch may have reached the hosted agent."""


class RepositoryTurnInvocationRejectedError(RuntimeError):
    """Raised when a reserved turn loses its fence before dispatch."""


class RepositoryTurnCompletionRejectedError(RuntimeError):
    """Raised when a stale attempt or deletion fence rejects completion."""


class RepositoryActiveTurnError(RuntimeError):
    """Raised after tombstoning when an in-flight turn must drain."""


class RepositoryInvariantError(RuntimeError):
    """Raised when committed persistence state violates an internal invariant."""


_T = TypeVar("_T")


class TransactionRunner(Protocol):
    """Abstraction over retrying Mongo transactions for offline unit tests."""

    async def run(self, operation: Callable[[Any], Awaitable[_T]]) -> _T: ...


class MongoTransactionRunner:
    """Run an operation in a retrying PyMongo transaction.

    Azure Cosmos DB for MongoDB vCore supports multi-document transactions. The
    boundary is isolated here so unit tests can inject a direct in-memory runner.
    """

    def __init__(self, client: AsyncMongoClient[Any]) -> None:
        self._client = client

    async def run(self, operation: Callable[[Any], Awaitable[_T]]) -> _T:
        async with self._client.start_session() as session:
            return cast(_T, await session.with_transaction(operation))


@dataclass(frozen=True, slots=True)
class Page(Generic[_T]):
    """A stable keyset page and its continuation cursor."""

    items: list[_T]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TurnReservation:
    """A newly fenced attempt or a completed replay."""

    turn: Turn
    user_message: Message | None = None
    assistant_message: Message | None = None

    @property
    def is_replay(self) -> bool:
        return self.turn.status is TurnStatus.COMPLETED


class ConversationRepository:
    """Read/write access to a single user's conversations and messages."""

    def __init__(
        self,
        conversations: AsyncCollection[Any],
        messages: AsyncCollection[Any],
        turns: AsyncCollection[Any],
        transactions: TransactionRunner,
        *,
        clock: Callable[[], datetime] | None = None,
        attempt_id_factory: Callable[[], str] | None = None,
        deletion_id_factory: Callable[[], str] | None = None,
        deletion_tombstone_retention_seconds: int = _DEFAULT_DELETION_TOMBSTONE_RETENTION_SECONDS,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._turns = turns
        self._transactions = transactions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._attempt_id_factory = attempt_id_factory or (lambda: str(uuid4()))
        self._deletion_id_factory = deletion_id_factory or (lambda: str(uuid4()))
        self._deletion_tombstone_retention_seconds = deletion_tombstone_retention_seconds

    # ----------------------------------------------------------------- #
    # Conversations
    # ----------------------------------------------------------------- #

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        await self._conversations.insert_one(_to_document(conversation))
        return conversation

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation | None:
        document = await self._conversations.find_one(
            _scope(
                user_id,
                {
                    "_id": conversation_id,
                    "state": {"$ne": ConversationState.DELETING.value},
                },
            )
        )
        if document is None:
            # Also the result when the conversation exists but belongs to someone
            # else, because the filter is user-scoped. Callers surface a plain 404
            # so the two cases are indistinguishable to the client.
            return None
        return Conversation.model_validate(_from_document(document))

    async def get_conversation_for_deletion(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        """Return an active or tombstoned conversation for retryable cleanup."""
        document = await self._conversations.find_one(_scope(user_id, {"_id": conversation_id}))
        if document is None or document.get("docType") == _TOMBSTONE_DOC_TYPE:
            return None
        return Conversation.model_validate(_from_document(document))

    async def list_conversations(
        self,
        user_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Conversation]:
        page_size = _clamp(limit)
        extra: dict[str, Any] = {"state": {"$ne": ConversationState.DELETING.value}}
        if cursor is not None:
            cursor_at, cursor_id = _decode_cursor(cursor)
            extra["$or"] = [
                {"updatedAt": {"$lt": cursor_at}},
                {"updatedAt": cursor_at, "_id": {"$lt": cursor_id}},
            ]

        mongo_cursor = (
            self._conversations.find(_scope(user_id, extra))
            .sort([("updatedAt", DESCENDING), ("_id", DESCENDING)])
            .limit(page_size + 1)
        )
        documents = [document async for document in mongo_cursor]
        page_documents = documents[:page_size]
        next_cursor = (
            _encode_cursor(page_documents[-1]["updatedAt"], str(page_documents[-1]["_id"]))
            if len(documents) > page_size
            else None
        )
        return Page(
            items=[
                Conversation.model_validate(_from_document(document)) for document in page_documents
            ],
            next_cursor=next_cursor,
        )

    async def delete_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        purge: bool = True,
    ) -> bool:
        """Tombstone, then optionally purge transcript and turn records."""
        now = self._clock()

        async def tombstone(session: Any) -> tuple[bool, bool, bool]:
            document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            if document is None:
                return False, False, False
            if document.get("docType") == _TOMBSTONE_DOC_TYPE:
                return True, False, True

            active_turn_id = document.get("activeTurnId")
            has_live_turn = False
            if isinstance(active_turn_id, str):
                turn_document = await self._turns.find_one(
                    _scope(
                        user_id,
                        {
                            "_id": active_turn_id,
                            "conversationId": conversation_id,
                            "status": {
                                "$in": [
                                    TurnStatus.PENDING.value,
                                    TurnStatus.INVOKING.value,
                                ]
                            },
                        },
                    ),
                    session=session,
                )
                if turn_document is not None:
                    turn = Turn.model_validate(_from_document(turn_document))
                    has_live_turn = _lease_is_active(turn, now)
                    if not has_live_turn:
                        expired_status = (
                            TurnStatus.INDETERMINATE
                            if turn.status is TurnStatus.INVOKING
                            else TurnStatus.FAILED
                        )
                        await self._turns.update_one(
                            _scope(
                                user_id,
                                {
                                    "_id": active_turn_id,
                                    "conversationId": conversation_id,
                                    "status": turn.status.value,
                                    "attemptId": turn.attempt_id,
                                },
                            ),
                            {
                                "$set": {
                                    "status": expired_status.value,
                                    "leaseExpiresAt": None,
                                    "failureCode": "deletion",
                                    "updatedAt": _timestamp(now),
                                }
                            },
                            session=session,
                        )

            result = await self._conversations.update_one(
                _scope(user_id, {"_id": conversation_id}),
                {
                    "$set": {
                        "state": ConversationState.DELETING.value,
                        "updatedAt": _timestamp(now),
                        "deletionRequestId": document.get("deletionRequestId")
                        or self._deletion_id_factory(),
                        "deletionRequestedAt": document.get("deletionRequestedAt")
                        or _timestamp(now),
                        **({} if has_live_turn else {"activeTurnId": None}),
                    },
                },
                session=session,
            )
            if result.matched_count != 1:
                raise RepositoryInvariantError("Conversation tombstone was not applied.")
            return True, has_live_turn, False

        exists, has_live_turn, already_deleted = await self._transactions.run(tombstone)
        if not exists:
            return False
        if already_deleted:
            return True
        if has_live_turn:
            raise RepositoryActiveTurnError(conversation_id)
        if not purge:
            return True

        async def purge_documents(session: Any) -> tuple[int, int, str]:
            document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            if document is None:
                raise RepositoryInvariantError("Conversation purge lost its tombstone.")
            if document.get("state") != ConversationState.DELETING.value:
                raise RepositoryInvariantError("Conversation purge requires a tombstone.")
            if document.get("activeTurnId") is not None:
                raise RepositoryActiveTurnError(conversation_id)

            pending = await self._turns.find_one(
                _scope(
                    user_id,
                    {
                        "conversationId": conversation_id,
                        "status": {
                            "$in": [
                                TurnStatus.PENDING.value,
                                TurnStatus.INVOKING.value,
                            ]
                        },
                    },
                ),
                session=session,
            )
            if pending is not None:
                raise RepositoryActiveTurnError(conversation_id)

            removed_messages = await self._messages.delete_many(
                _scope(user_id, {"conversationId": conversation_id}),
                session=session,
            )
            removed_turns = await self._turns.delete_many(
                _scope(user_id, {"conversationId": conversation_id}),
                session=session,
            )
            deletion_request_id = document.get("deletionRequestId")
            deletion_requested_at = document.get("deletionRequestedAt")
            if not isinstance(deletion_request_id, str) or not deletion_request_id:
                raise RepositoryInvariantError("Deletion tombstone is missing its request id.")
            if not isinstance(deletion_requested_at, str) or not deletion_requested_at:
                raise RepositoryInvariantError("Deletion tombstone is missing its request time.")

            expires_at = now + timedelta(seconds=self._deletion_tombstone_retention_seconds)
            terminal_tombstone = {
                "_id": conversation_id,
                "userId": user_id,
                "state": ConversationState.DELETING.value,
                "deletionRequestId": deletion_request_id,
                "deletionRequestedAt": deletion_requested_at,
                "deletedAt": _timestamp(now),
                # MongoDB TTL indexes require a BSON datetime, not an ISO string.
                "expiresAt": expires_at,
                "docType": _TOMBSTONE_DOC_TYPE,
            }
            replaced_conversation = await self._conversations.replace_one(
                _scope(
                    user_id,
                    {
                        "_id": conversation_id,
                        "state": ConversationState.DELETING.value,
                        "activeTurnId": None,
                    },
                ),
                terminal_tombstone,
                session=session,
            )
            if replaced_conversation.matched_count != 1:
                raise RepositoryInvariantError("Conversation purge lost its tombstone fence.")
            return (
                removed_messages.deleted_count,
                removed_turns.deleted_count,
                deletion_request_id,
            )

        messages_removed, turns_removed, deletion_request_id = await self._transactions.run(
            purge_documents
        )
        _LOGGER.info(
            "conversation deleted",
            extra={
                "conversation_id": conversation_id,
                "deletion_request_id": deletion_request_id,
                "messages_removed": messages_removed,
                "turns_removed": turns_removed,
            },
        )
        return True

    # ----------------------------------------------------------------- #
    # Messages
    # ----------------------------------------------------------------- #

    async def add_message(self, message: Message) -> Message:
        await self._messages.insert_one(_to_document(message))
        return message

    async def list_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Message]:
        page_size = _clamp(limit)
        extra: dict[str, Any] = {"conversationId": conversation_id}
        if cursor is not None:
            cursor_at, cursor_id = _decode_cursor(cursor)
            extra["$or"] = [
                {"createdAt": {"$lt": cursor_at}},
                {"createdAt": cursor_at, "_id": {"$lt": cursor_id}},
            ]

        mongo_cursor = (
            self._messages.find(_scope(user_id, extra))
            .sort([("createdAt", DESCENDING), ("_id", DESCENDING)])
            .limit(page_size + 1)
        )
        documents = [document async for document in mongo_cursor]
        page_documents = documents[:page_size]
        next_cursor = (
            _encode_cursor(page_documents[-1]["createdAt"], str(page_documents[-1]["_id"]))
            if len(documents) > page_size
            else None
        )
        return Page(
            items=[
                Message.model_validate(_from_document(document))
                for document in reversed(page_documents)
            ],
            next_cursor=next_cursor,
        )

    async def list_recent_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int,
    ) -> list[Message]:
        """Return the newest committed message window in chronological order."""
        mongo_cursor = (
            self._messages.find(_scope(user_id, {"conversationId": conversation_id}))
            .sort([("createdAt", DESCENDING), ("_id", DESCENDING)])
            .limit(_clamp(limit))
        )
        newest_first = [
            Message.model_validate(_from_document(document)) async for document in mongo_cursor
        ]
        newest_first.reverse()
        return newest_first

    # ----------------------------------------------------------------- #
    # Idempotent turn state machine
    # ----------------------------------------------------------------- #

    async def reserve_turn(
        self,
        user_id: str,
        conversation_id: str,
        idempotency_key: str,
        content_hash: str,
        *,
        lease_seconds: int,
    ) -> TurnReservation:
        """Fence one model call or replay its previously committed result."""
        now = self._clock()
        attempt_id = self._attempt_id_factory()
        turn_id = _turn_id(user_id, conversation_id, idempotency_key)
        lease_expires_at = now + timedelta(seconds=lease_seconds)

        async def reserve(session: Any) -> TurnReservation:
            conversation_document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            if conversation_document is None:
                raise RepositoryConversationNotFoundError(conversation_id)
            if (
                conversation_document.get("state", ConversationState.ACTIVE.value)
                == ConversationState.DELETING.value
            ):
                raise RepositoryConversationDeletingError(conversation_id)

            turn_document = await self._turns.find_one(
                _scope(user_id, {"_id": turn_id, "conversationId": conversation_id}),
                session=session,
            )
            if turn_document is not None:
                turn = Turn.model_validate(_from_document(turn_document))
                if turn.content_hash != content_hash:
                    raise RepositoryTurnConflictError(idempotency_key)
                if turn.status is TurnStatus.COMPLETED:
                    if (
                        conversation_document.get(
                            "conversationManager", ConversationManager.COSMOS.value
                        )
                        == ConversationManager.FOUNDRY.value
                    ):
                        return TurnReservation(turn=turn)
                    return await self._load_completed_reservation(turn, session)
                if turn.status in {TurnStatus.PENDING, TurnStatus.INVOKING} and _lease_is_active(
                    turn, now
                ):
                    if turn.lease_expires_at is None:
                        raise RepositoryInvariantError("Active turn lease has no expiry.")
                    retry_after = max(
                        1,
                        math.ceil((turn.lease_expires_at - now).total_seconds()),
                    )
                    raise RepositoryTurnPendingError(retry_after)
                if turn.status in {TurnStatus.INVOKING, TurnStatus.INDETERMINATE}:
                    if turn.status is TurnStatus.INVOKING:
                        result = await self._turns.update_one(
                            _scope(
                                user_id,
                                {
                                    "_id": turn_id,
                                    "conversationId": conversation_id,
                                    "status": TurnStatus.INVOKING.value,
                                    "attemptId": turn.attempt_id,
                                },
                            ),
                            {
                                "$set": {
                                    "status": TurnStatus.INDETERMINATE.value,
                                    "leaseExpiresAt": None,
                                    "failureCode": "invocation_outcome_unknown",
                                    "updatedAt": _timestamp(now),
                                }
                            },
                            session=session,
                        )
                        if result.matched_count != 1:
                            raise RepositoryInvariantError(
                                "Expired invocation lost its attempt fence."
                            )
                        turn = turn.model_copy(
                            update={
                                "status": TurnStatus.INDETERMINATE,
                                "lease_expires_at": None,
                                "failure_code": "invocation_outcome_unknown",
                                "updated_at": now,
                            }
                        )
                    await self._conversations.update_one(
                        _scope(user_id, {"_id": conversation_id, "activeTurnId": turn_id}),
                        {"$set": {"activeTurnId": None}},
                        session=session,
                    )
                    return TurnReservation(turn=turn)

            await self._acquire_turn_fence(user_id, conversation_id, turn_id, now, session)

            if turn_document is None:
                turn = Turn(
                    id=turn_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    content_hash=content_hash,
                    status=TurnStatus.PENDING,
                    attempt_id=attempt_id,
                    lease_expires_at=lease_expires_at,
                    created_at=now,
                    updated_at=now,
                )
                await self._turns.insert_one(_to_document(turn), session=session)
                return TurnReservation(turn=turn)

            prior_turn = Turn.model_validate(_from_document(turn_document))
            result = await self._turns.update_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": prior_turn.status.value,
                        "attemptId": prior_turn.attempt_id,
                    },
                ),
                {
                    "$set": {
                        "status": TurnStatus.PENDING.value,
                        "attemptId": attempt_id,
                        "leaseExpiresAt": _timestamp(lease_expires_at),
                        "updatedAt": _timestamp(now),
                        "failureCode": None,
                    }
                },
                session=session,
            )
            if result.matched_count != 1:
                raise RepositoryInvariantError("Turn retry transition lost its attempt fence.")

            return TurnReservation(
                turn=prior_turn.model_copy(
                    update={
                        "status": TurnStatus.PENDING,
                        "attempt_id": attempt_id,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                        "failure_code": None,
                    }
                )
            )

        reservation: TurnReservation | None = None
        for duplicate_retry in range(2):
            try:
                reservation = await self._transactions.run(reserve)
                break
            except DuplicateKeyError:
                if duplicate_retry == 1:
                    raise
        if reservation is None:
            raise RepositoryInvariantError("Turn reservation retry was exhausted.")
        if reservation.turn.status is TurnStatus.INDETERMINATE:
            raise RepositoryTurnIndeterminateError(idempotency_key)
        return reservation

    async def begin_turn_invocation(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
    ) -> None:
        """Persist the at-most-once boundary before dispatching to Foundry."""
        now = self._clock()

        async def begin(session: Any) -> None:
            conversation_document = await self._conversations.find_one(
                _scope(
                    user_id,
                    {
                        "_id": conversation_id,
                        "state": {"$ne": ConversationState.DELETING.value},
                        "activeTurnId": turn_id,
                    },
                ),
                session=session,
            )
            if conversation_document is None:
                raise RepositoryTurnInvocationRejectedError(conversation_id)

            result = await self._turns.update_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": TurnStatus.PENDING.value,
                        "attemptId": attempt_id,
                    },
                ),
                {
                    "$set": {
                        "status": TurnStatus.INVOKING.value,
                        "updatedAt": _timestamp(now),
                    }
                },
                session=session,
            )
            if result.matched_count != 1:
                raise RepositoryTurnInvocationRejectedError(turn_id)

        await self._transactions.run(begin)

    async def complete_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        user_message: Message,
        assistant_message: Message,
        *,
        title: str | None,
        persist_messages: bool = True,
    ) -> None:
        """Atomically commit turn references, metadata, and optional transcript."""
        now = self._clock()

        async def complete(session: Any) -> None:
            conversation_document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            if (
                conversation_document is None
                or conversation_document.get("state", ConversationState.ACTIVE.value)
                != ConversationState.ACTIVE.value
                or conversation_document.get("activeTurnId") != turn_id
            ):
                raise RepositoryTurnCompletionRejectedError(conversation_id)

            turn_document = await self._turns.find_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": TurnStatus.INVOKING.value,
                        "attemptId": attempt_id,
                    },
                ),
                session=session,
            )
            if turn_document is None:
                raise RepositoryTurnCompletionRejectedError(turn_id)

            previous_updated_at = Conversation.model_validate(
                _from_document(conversation_document)
            ).updated_at
            completed_at = max(now, previous_updated_at + timedelta(microseconds=1))
            if persist_messages:
                user_message.created_at = completed_at
                assistant_message.created_at = completed_at + timedelta(microseconds=1)
                completed_at = assistant_message.created_at
                await self._messages.insert_one(_to_document(user_message), session=session)
                await self._messages.insert_one(_to_document(assistant_message), session=session)

            conversation_set: dict[str, Any] = {
                "updatedAt": _timestamp(completed_at),
                "activeTurnId": None,
            }
            if (
                title is not None
                and conversation_document.get("messageCount", 0) == 0
                and conversation_document.get("title") == "New conversation"
            ):
                conversation_set["title"] = title

            conversation_result = await self._conversations.update_one(
                _scope(
                    user_id,
                    {
                        "_id": conversation_id,
                        "state": ConversationState.ACTIVE.value,
                        "activeTurnId": turn_id,
                    },
                ),
                {
                    "$inc": {"messageCount": 2},
                    "$set": conversation_set,
                },
                session=session,
            )
            if conversation_result.matched_count != 1:
                raise RepositoryTurnCompletionRejectedError(conversation_id)

            turn_result = await self._turns.update_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": TurnStatus.INVOKING.value,
                        "attemptId": attempt_id,
                    },
                ),
                {
                    "$set": {
                        "status": TurnStatus.COMPLETED.value,
                        "leaseExpiresAt": None,
                        "userMessageId": user_message.id,
                        "assistantMessageId": assistant_message.id,
                        "failureCode": None,
                        "updatedAt": _timestamp(now),
                    }
                },
                session=session,
            )
            if turn_result.matched_count != 1:
                raise RepositoryTurnCompletionRejectedError(turn_id)

        await self._transactions.run(complete)

    async def mark_turn_indeterminate(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        failure_code: str,
    ) -> bool:
        """Fence a post-dispatch failure from automatic model reinvocation."""
        now = self._clock()

        async def mark(session: Any) -> tuple[bool, bool]:
            turn_result = await self._turns.update_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": TurnStatus.INVOKING.value,
                        "attemptId": attempt_id,
                    },
                ),
                {
                    "$set": {
                        "status": TurnStatus.INDETERMINATE.value,
                        "leaseExpiresAt": None,
                        "failureCode": failure_code,
                        "updatedAt": _timestamp(now),
                    }
                },
                session=session,
            )
            if turn_result.matched_count != 1:
                return False, False

            await self._conversations.update_one(
                _scope(user_id, {"_id": conversation_id, "activeTurnId": turn_id}),
                {"$set": {"activeTurnId": None}},
                session=session,
            )
            conversation_document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            deletion_pending = (
                conversation_document is not None
                and conversation_document.get("state") == ConversationState.DELETING.value
                and conversation_document.get("docType") != _TOMBSTONE_DOC_TYPE
                and conversation_document.get(
                    "conversationManager", ConversationManager.COSMOS.value
                )
                == ConversationManager.COSMOS.value
            )
            return True, deletion_pending

        marked, deletion_pending = await self._transactions.run(mark)
        if deletion_pending:
            await self.delete_conversation(user_id, conversation_id)
        return marked

    async def fail_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        attempt_id: str,
        failure_code: str,
    ) -> bool:
        """Fail a pre-dispatch attempt and release its conversation fence."""
        now = self._clock()

        async def fail(session: Any) -> tuple[bool, bool]:
            turn_result = await self._turns.update_one(
                _scope(
                    user_id,
                    {
                        "_id": turn_id,
                        "conversationId": conversation_id,
                        "status": TurnStatus.PENDING.value,
                        "attemptId": attempt_id,
                    },
                ),
                {
                    "$set": {
                        "status": TurnStatus.FAILED.value,
                        "leaseExpiresAt": None,
                        "failureCode": failure_code,
                        "updatedAt": _timestamp(now),
                    }
                },
                session=session,
            )
            if turn_result.matched_count != 1:
                return False, False

            conversation_result = await self._conversations.update_one(
                _scope(user_id, {"_id": conversation_id, "activeTurnId": turn_id}),
                {"$set": {"activeTurnId": None}},
                session=session,
            )
            if conversation_result.matched_count != 1:
                raise RepositoryInvariantError("Failed turn did not own its conversation fence.")
            conversation_document = await self._conversations.find_one(
                _scope(user_id, {"_id": conversation_id}),
                session=session,
            )
            deletion_pending = (
                conversation_document is not None
                and conversation_document.get("state") == ConversationState.DELETING.value
                and conversation_document.get("docType") != _TOMBSTONE_DOC_TYPE
                and conversation_document.get(
                    "conversationManager", ConversationManager.COSMOS.value
                )
                == ConversationManager.COSMOS.value
            )
            return True, deletion_pending

        failed, deletion_pending = await self._transactions.run(fail)
        if deletion_pending:
            await self.delete_conversation(user_id, conversation_id)
        return failed

    async def _acquire_turn_fence(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        now: datetime,
        session: Any,
    ) -> None:
        result = await self._conversations.update_one(
            _scope(
                user_id,
                {
                    "_id": conversation_id,
                    "state": {"$ne": ConversationState.DELETING.value},
                    "activeTurnId": {"$in": [None, turn_id]},
                },
            ),
            {"$set": {"state": ConversationState.ACTIVE.value, "activeTurnId": turn_id}},
            session=session,
        )
        if result.matched_count == 1:
            return

        conversation_document = await self._conversations.find_one(
            _scope(user_id, {"_id": conversation_id}),
            session=session,
        )
        if conversation_document is None:
            raise RepositoryConversationNotFoundError(conversation_id)
        if conversation_document.get("state") == ConversationState.DELETING.value:
            raise RepositoryConversationDeletingError(conversation_id)

        active_turn_id = conversation_document.get("activeTurnId")
        if not isinstance(active_turn_id, str):
            raise RepositoryConversationBusyError(conversation_id)
        active_document = await self._turns.find_one(
            _scope(
                user_id,
                {"_id": active_turn_id, "conversationId": conversation_id},
            ),
            session=session,
        )
        if active_document is not None:
            active_turn = Turn.model_validate(_from_document(active_document))
            if active_turn.status in {TurnStatus.PENDING, TurnStatus.INVOKING}:
                if _lease_is_active(active_turn, now):
                    raise RepositoryConversationBusyError(conversation_id)
                expired_status = (
                    TurnStatus.INDETERMINATE
                    if active_turn.status is TurnStatus.INVOKING
                    else TurnStatus.FAILED
                )
                expired_result = await self._turns.update_one(
                    _scope(
                        user_id,
                        {
                            "_id": active_turn_id,
                            "conversationId": conversation_id,
                            "status": active_turn.status.value,
                            "attemptId": active_turn.attempt_id,
                        },
                    ),
                    {
                        "$set": {
                            "status": expired_status.value,
                            "leaseExpiresAt": None,
                            "failureCode": "lease_expired",
                            "updatedAt": _timestamp(now),
                        }
                    },
                    session=session,
                )
                if expired_result.matched_count != 1:
                    raise RepositoryInvariantError("Expired turn lost its attempt fence.")

        recovered = await self._conversations.update_one(
            _scope(
                user_id,
                {
                    "_id": conversation_id,
                    "state": {"$ne": ConversationState.DELETING.value},
                    "activeTurnId": active_turn_id,
                },
            ),
            {"$set": {"state": ConversationState.ACTIVE.value, "activeTurnId": turn_id}},
            session=session,
        )
        if recovered.matched_count != 1:
            raise RepositoryConversationBusyError(conversation_id)

    async def _load_completed_reservation(
        self,
        turn: Turn,
        session: Any,
    ) -> TurnReservation:
        if turn.user_message_id is None or turn.assistant_message_id is None:
            raise RepositoryInvariantError("Completed turn is missing message references.")

        user_document = await self._messages.find_one(
            _scope(
                turn.user_id,
                {"_id": turn.user_message_id, "conversationId": turn.conversation_id},
            ),
            session=session,
        )
        assistant_document = await self._messages.find_one(
            _scope(
                turn.user_id,
                {"_id": turn.assistant_message_id, "conversationId": turn.conversation_id},
            ),
            session=session,
        )
        if user_document is None or assistant_document is None:
            raise RepositoryInvariantError("Completed turn transcript is missing.")

        return TurnReservation(
            turn=turn,
            user_message=Message.model_validate(_from_document(user_document)),
            assistant_message=Message.model_validate(_from_document(assistant_document)),
        )


# --------------------------------------------------------------------------- #
# Document mapping
# --------------------------------------------------------------------------- #


def _scope(user_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a filter that is always constrained to one user.

    Every query in this module goes through here. That is what makes cross-user
    reads impossible rather than merely unlikely.
    """
    query = dict(extra or {})
    # Apply the trusted scope last so caller-supplied criteria can never replace it.
    query["userId"] = user_id
    return query


def _to_document(model: Conversation | Message | Turn) -> dict[str, Any]:
    """Serialise a domain model, mapping `id` onto MongoDB's `_id`."""
    document = model.model_dump(mode="json", by_alias=True)
    document["_id"] = document.pop("id")
    return document


def _from_document(document: dict[str, Any]) -> dict[str, Any]:
    """Reverse `_to_document`."""
    restored = dict(document)
    restored["id"] = restored.pop("_id")
    return restored


def _clamp(limit: int) -> int:
    return max(1, min(limit, _MAX_PAGE_SIZE))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _turn_id(user_id: str, conversation_id: str, idempotency_key: str) -> str:
    material = "\0".join((user_id, conversation_id, idempotency_key)).encode()
    return f"turn:{hashlib.sha256(material).hexdigest()}"


def _lease_is_active(turn: Turn, now: datetime) -> bool:
    return turn.lease_expires_at is not None and turn.lease_expires_at > now


def _encode_cursor(timestamp: Any, document_id: str) -> str:
    if isinstance(timestamp, datetime):
        timestamp_text = _timestamp(timestamp)
    elif isinstance(timestamp, str):
        timestamp_text = timestamp
    else:
        raise RepositoryInvariantError("Pagination key is not a timestamp.")

    payload = json.dumps(
        {"v": _CURSOR_VERSION, "at": timestamp_text, "id": document_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        if not cursor or len(cursor) > 512:
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        timestamp = payload.get("at")
        document_id = payload.get("id")
        if not isinstance(timestamp, str) or not isinstance(document_id, str) or not document_id:
            raise ValueError
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("Invalid pagination cursor.") from exc
    return timestamp, document_id


# --------------------------------------------------------------------------- #
# Client lifetime
# --------------------------------------------------------------------------- #


class MongoResources:
    """Owns the Mongo client lifetime and hands out a repository.

    The client is created once at application start-up and closed at shutdown.
    Creating one per request would rebuild the connection pool and repeat the
    OIDC handshake on every call.
    """

    def __init__(
        self,
        connection_string: str,
        credential: TokenCredential,
        database_name: str,
        conversations_collection: str,
        messages_collection: str,
        turns_collection: str,
        deletion_tombstone_retention_seconds: int,
    ) -> None:
        self._client: AsyncMongoClient[Any] = AsyncMongoClient(
            connection_string,
            authMechanism="MONGODB-OIDC",
            authMechanismProperties={
                "OIDC_CALLBACK": AzureIdentityTokenCallback(credential),
            },
            tls=True,
            connectTimeoutMS=120_000,
            serverSelectionTimeoutMS=30_000,
        )
        self._database_name = database_name
        self._conversations_collection = conversations_collection
        self._messages_collection = messages_collection
        self._turns_collection = turns_collection
        self._deletion_tombstone_retention_seconds = deletion_tombstone_retention_seconds
        self._transactions = MongoTransactionRunner(self._client)

    @property
    def database(self) -> AsyncDatabase[Any]:
        return self._client[self._database_name]

    def repository(self) -> ConversationRepository:
        database = self.database
        return ConversationRepository(
            conversations=database[self._conversations_collection],
            messages=database[self._messages_collection],
            turns=database[self._turns_collection],
            transactions=self._transactions,
            deletion_tombstone_retention_seconds=(self._deletion_tombstone_retention_seconds),
        )

    async def ensure_schema(self) -> None:
        """Create the collections and indexes if they are absent.

        MongoDB creates a database and collection lazily on first write, which
        would leave the indexes missing. Creating them explicitly at start-up makes
        the shape deterministic. Both operations are idempotent.
        """
        database = self.database
        existing = set(await database.list_collection_names())

        for name in (
            self._conversations_collection,
            self._messages_collection,
            self._turns_collection,
        ):
            if name not in existing:
                await database.create_collection(name)
                _LOGGER.info("created collection", extra={"collection": name})

        # Every read is (userId) or (userId, conversationId); these back both.
        await database[self._conversations_collection].create_index(
            [("userId", ASCENDING), ("updatedAt", DESCENDING), ("_id", DESCENDING)],
            name="user_updated_id",
        )
        await database[self._messages_collection].create_index(
            [
                ("userId", ASCENDING),
                ("conversationId", ASCENDING),
                ("createdAt", ASCENDING),
                ("_id", ASCENDING),
            ],
            name="user_conversation_created_id",
        )
        await database[self._turns_collection].create_index(
            [
                ("userId", ASCENDING),
                ("conversationId", ASCENDING),
                ("idempotencyKey", ASCENDING),
            ],
            name="user_conversation_idempotency",
            unique=True,
        )
        await database[self._turns_collection].create_index(
            [("userId", ASCENDING), ("conversationId", ASCENDING), ("status", ASCENDING)],
            name="user_conversation_status",
        )
        await database[self._conversations_collection].create_index(
            [("expiresAt", ASCENDING)],
            name="deletion_tombstone_expiry",
            expireAfterSeconds=0,
        )

    async def ping(self) -> None:
        """Verify connectivity. Used by the readiness probe."""
        await self._client.admin.command("ping")

    async def close(self) -> None:
        await self._client.close()
