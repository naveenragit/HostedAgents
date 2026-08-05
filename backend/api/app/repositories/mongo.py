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

import logging
from typing import Any, Final

from azure.core.credentials import TokenCredential
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.auth_oidc import OIDCCallback, OIDCCallbackContext, OIDCCallbackResult

from app.domain.models import Conversation, Message

_LOGGER = logging.getLogger("api.repository")

_MAX_PAGE_SIZE: Final[int] = 200

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


class ConversationRepository:
    """Read/write access to a single user's conversations and messages."""

    def __init__(self, conversations: AsyncCollection[Any], messages: AsyncCollection[Any]) -> None:
        self._conversations = conversations
        self._messages = messages

    # ----------------------------------------------------------------- #
    # Conversations
    # ----------------------------------------------------------------- #

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        await self._conversations.insert_one(_to_document(conversation))
        return conversation

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation | None:
        document = await self._conversations.find_one(_scope(user_id, {"_id": conversation_id}))
        if document is None:
            # Also the result when the conversation exists but belongs to someone
            # else, because the filter is user-scoped. Callers surface a plain 404
            # so the two cases are indistinguishable to the client.
            return None
        return Conversation.model_validate(_from_document(document))

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[Conversation]:
        cursor = (
            self._conversations.find(_scope(user_id))
            .sort("updatedAt", DESCENDING)
            .limit(_clamp(limit))
        )
        return [Conversation.model_validate(_from_document(doc)) async for doc in cursor]

    async def update_conversation(self, conversation: Conversation) -> Conversation:
        document = _to_document(conversation)
        await self._conversations.replace_one(
            _scope(conversation.user_id, {"_id": conversation.id}),
            document,
            upsert=False,
        )
        return conversation

    async def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        """Delete a conversation and its messages. Returns False if it did not exist."""
        result = await self._conversations.delete_one(_scope(user_id, {"_id": conversation_id}))
        if result.deleted_count == 0:
            return False

        removed = await self._messages.delete_many(
            _scope(user_id, {"conversationId": conversation_id})
        )
        _LOGGER.info(
            "conversation deleted",
            extra={"conversation_id": conversation_id, "messages_removed": removed.deleted_count},
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
        limit: int = 100,
    ) -> list[Message]:
        cursor = (
            self._messages.find(_scope(user_id, {"conversationId": conversation_id}))
            .sort("createdAt", ASCENDING)
            .limit(_clamp(limit))
        )
        return [Message.model_validate(_from_document(doc)) async for doc in cursor]


# --------------------------------------------------------------------------- #
# Document mapping
# --------------------------------------------------------------------------- #


def _scope(user_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a filter that is always constrained to one user.

    Every query in this module goes through here. That is what makes cross-user
    reads impossible rather than merely unlikely.
    """
    query: dict[str, Any] = {"userId": user_id}
    if extra:
        query.update(extra)
    return query


def _to_document(model: Conversation | Message) -> dict[str, Any]:
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

    @property
    def database(self) -> AsyncDatabase[Any]:
        return self._client[self._database_name]

    def repository(self) -> ConversationRepository:
        database = self.database
        return ConversationRepository(
            conversations=database[self._conversations_collection],
            messages=database[self._messages_collection],
        )

    async def ensure_schema(self) -> None:
        """Create the collections and indexes if they are absent.

        MongoDB creates a database and collection lazily on first write, which
        would leave the indexes missing. Creating them explicitly at start-up makes
        the shape deterministic. Both operations are idempotent.
        """
        database = self.database
        existing = set(await database.list_collection_names())

        for name in (self._conversations_collection, self._messages_collection):
            if name not in existing:
                await database.create_collection(name)
                _LOGGER.info("created collection", extra={"collection": name})

        # Every read is (userId) or (userId, conversationId); these back both.
        await database[self._conversations_collection].create_index(
            [("userId", ASCENDING), ("updatedAt", DESCENDING)],
            name="user_updated",
        )
        await database[self._messages_collection].create_index(
            [("userId", ASCENDING), ("conversationId", ASCENDING), ("createdAt", ASCENDING)],
            name="user_conversation_created",
        )

    async def ping(self) -> None:
        """Verify connectivity. Used by the readiness probe."""
        await self._client.admin.command("ping")

    async def close(self) -> None:
        await self._client.close()
