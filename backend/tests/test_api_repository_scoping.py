"""Tests for the per-user scoping rules in the Mongo repository.

These cover the property that matters most for security: no query can be issued
without a `userId` constraint. They exercise the filter construction and document
mapping directly, with a fake collection — no database, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.models import Conversation, Message, Role
from app.repositories.mongo import ConversationRepository, _from_document, _scope, _to_document

USER = "user-alpha"
OTHER_USER = "user-beta"


class FakeCursor:
    """Minimal stand-in for an async Mongo cursor."""

    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = documents

    def sort(self, *_args: Any, **_kwargs: Any) -> FakeCursor:
        return self

    def limit(self, _n: int) -> FakeCursor:
        return self

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for document in self._documents:
                yield document

        return gen()


class FakeCollection:
    """Records the filters it is called with so tests can assert on them."""

    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = documents or []
        self.filters: list[dict[str, Any]] = []

    async def insert_one(self, document: dict[str, Any]) -> None:
        self.documents.append(document)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self.filters.append(query)
        return next(
            (d for d in self.documents if all(d.get(k) == v for k, v in query.items())),
            None,
        )

    def find(self, query: dict[str, Any]) -> FakeCursor:
        self.filters.append(query)
        return FakeCursor(
            [d for d in self.documents if all(d.get(k) == v for k, v in query.items())]
        )

    async def delete_one(self, query: dict[str, Any]) -> Any:
        self.filters.append(query)
        matched = [d for d in self.documents if all(d.get(k) == v for k, v in query.items())]
        for d in matched:
            self.documents.remove(d)
        return type("Result", (), {"deleted_count": len(matched)})()

    async def delete_many(self, query: dict[str, Any]) -> Any:
        return await self.delete_one(query)

    async def replace_one(self, query: dict[str, Any], document: dict[str, Any], **_: Any) -> None:
        self.filters.append(query)
        self.documents = [
            document if all(d.get(k) == v for k, v in query.items()) else d for d in self.documents
        ]


def _conversation(user_id: str = USER) -> Conversation:
    return Conversation(user_id=user_id, agent_name="research-agent", title="Test")


# --------------------------------------------------------------------------- #
# Filter construction
# --------------------------------------------------------------------------- #


def test_scope_always_includes_user_id() -> None:
    assert _scope(USER) == {"userId": USER}


def test_scope_merges_extra_criteria() -> None:
    assert _scope(USER, {"_id": "abc"}) == {"userId": USER, "_id": "abc"}


def test_scope_cannot_be_used_without_a_user() -> None:
    # A caller that forgets the user id gets a TypeError, not a global query.
    with pytest.raises(TypeError):
        _scope()  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Document mapping
# --------------------------------------------------------------------------- #


def test_to_document_maps_id_onto_mongo_id() -> None:
    conversation = _conversation()
    document = _to_document(conversation)

    assert document["_id"] == conversation.id
    assert "id" not in document
    assert document["userId"] == USER


def test_document_mapping_round_trips() -> None:
    conversation = _conversation()
    restored = Conversation.model_validate(_from_document(_to_document(conversation)))

    assert restored.id == conversation.id
    assert restored.user_id == conversation.user_id
    assert restored.agent_name == conversation.agent_name


# --------------------------------------------------------------------------- #
# Repository behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_conversation_scopes_query_to_the_caller() -> None:
    conversations = FakeCollection()
    repository = ConversationRepository(conversations, FakeCollection())  # type: ignore[arg-type]

    await repository.get_conversation(USER, "conversation-1")

    assert conversations.filters == [{"userId": USER, "_id": "conversation-1"}]


@pytest.mark.asyncio
async def test_another_users_conversation_is_not_returned() -> None:
    owned = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([owned])
    repository = ConversationRepository(conversations, FakeCollection())  # type: ignore[arg-type]

    result = await repository.get_conversation(USER, owned["_id"])

    assert result is None


@pytest.mark.asyncio
async def test_list_conversations_is_scoped() -> None:
    mine = _to_document(_conversation(USER))
    theirs = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([mine, theirs])
    repository = ConversationRepository(conversations, FakeCollection())  # type: ignore[arg-type]

    results = await repository.list_conversations(USER)

    assert [c.id for c in results] == [mine["_id"]]


@pytest.mark.asyncio
async def test_list_messages_is_scoped_to_user_and_conversation() -> None:
    messages = FakeCollection()
    repository = ConversationRepository(FakeCollection(), messages)  # type: ignore[arg-type]

    await repository.list_messages(USER, "conversation-1")

    assert messages.filters == [{"userId": USER, "conversationId": "conversation-1"}]


@pytest.mark.asyncio
async def test_delete_returns_false_for_another_users_conversation() -> None:
    theirs = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([theirs])
    repository = ConversationRepository(conversations, FakeCollection())  # type: ignore[arg-type]

    assert await repository.delete_conversation(USER, theirs["_id"]) is False
    # The other user's document survives.
    assert conversations.documents == [theirs]


@pytest.mark.asyncio
async def test_add_message_persists_scoped_document() -> None:
    messages = FakeCollection()
    repository = ConversationRepository(FakeCollection(), messages)  # type: ignore[arg-type]
    message = Message(
        user_id=USER,
        conversation_id="conversation-1",
        role=Role.USER,
        content="hello",
    )

    await repository.add_message(message)

    assert messages.documents[0]["userId"] == USER
    assert messages.documents[0]["_id"] == message.id
