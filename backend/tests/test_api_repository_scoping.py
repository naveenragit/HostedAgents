"""Tests for the per-user scoping rules in the Mongo repository.

These cover the property that matters most for security: no query can be issued
without a `userId` constraint. They exercise the filter construction and document
mapping directly, with a fake collection — no database, no network.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.domain.models import Conversation, ConversationState, Message, Role
from app.repositories.mongo import ConversationRepository, _from_document, _scope, _to_document

USER = "user-alpha"
OTHER_USER = "user-beta"


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


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

    async def insert_one(self, document: dict[str, Any], **_kwargs: Any) -> None:
        self.documents.append(document)

    async def find_one(
        self,
        query: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        self.filters.append(query)
        return next((document for document in self.documents if _matches(document, query)), None)

    def find(self, query: dict[str, Any]) -> FakeCursor:
        self.filters.append(query)
        return FakeCursor([document for document in self.documents if _matches(document, query)])

    async def delete_one(self, query: dict[str, Any], **_kwargs: Any) -> Any:
        self.filters.append(query)
        matched = [document for document in self.documents if _matches(document, query)]
        for document in matched:
            self.documents.remove(document)
        return type("Result", (), {"deleted_count": len(matched)})()

    async def delete_many(self, query: dict[str, Any], **kwargs: Any) -> Any:
        return await self.delete_one(query, **kwargs)

    async def replace_one(self, query: dict[str, Any], document: dict[str, Any], **_: Any) -> None:
        self.filters.append(query)
        self.documents = [
            document if all(d.get(k) == v for k, v in query.items()) else d for d in self.documents
        ]


class DirectTransactions:
    """Runs transaction callbacks directly; collection fakes provide atomic test state."""

    async def run(self, operation: Any) -> Any:
        return await operation(None)


def _repository(
    conversations: FakeCollection | None = None,
    messages: FakeCollection | None = None,
    turns: FakeCollection | None = None,
) -> ConversationRepository:
    return ConversationRepository(
        cast(Any, conversations or FakeCollection()),
        cast(Any, messages or FakeCollection()),
        cast(Any, turns or FakeCollection()),
        DirectTransactions(),
    )


def _conversation(user_id: str = USER) -> Conversation:
    return Conversation(user_id=user_id, agent_name="research-agent", title="Test")


# --------------------------------------------------------------------------- #
# Filter construction
# --------------------------------------------------------------------------- #


def test_scope_always_includes_user_id() -> None:
    assert _scope(USER) == {"userId": USER}


def test_scope_merges_extra_criteria() -> None:
    assert _scope(USER, {"_id": "abc"}) == {"userId": USER, "_id": "abc"}


def test_scope_extra_cannot_override_user_id() -> None:
    assert _scope(USER, {"userId": OTHER_USER, "_id": "abc"}) == {
        "userId": USER,
        "_id": "abc",
    }


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
    repository = _repository(conversations=conversations)

    await repository.get_conversation(USER, "conversation-1")

    assert conversations.filters == [
        {
            "userId": USER,
            "_id": "conversation-1",
            "state": {"$ne": "deleting"},
        }
    ]


@pytest.mark.asyncio
async def test_another_users_conversation_is_not_returned() -> None:
    owned = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([owned])
    repository = _repository(conversations=conversations)

    result = await repository.get_conversation(USER, owned["_id"])

    assert result is None


@pytest.mark.asyncio
async def test_list_conversations_is_scoped() -> None:
    mine = _to_document(_conversation(USER))
    theirs = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([mine, theirs])
    repository = _repository(conversations=conversations)

    page = await repository.list_conversations(USER)

    assert [conversation.id for conversation in page.items] == [mine["_id"]]
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_deleting_conversations_are_hidden_from_get_and_list() -> None:
    deleting = _to_document(
        _conversation(USER).model_copy(update={"state": ConversationState.DELETING})
    )
    conversations = FakeCollection([deleting])
    repository = _repository(conversations=conversations)

    result = await repository.get_conversation(USER, deleting["_id"])
    page = await repository.list_conversations(USER)

    assert result is None
    assert page.items == []


@pytest.mark.asyncio
async def test_list_messages_is_scoped_to_user_and_conversation() -> None:
    messages = FakeCollection()
    repository = _repository(messages=messages)

    await repository.list_messages(USER, "conversation-1")

    assert messages.filters == [{"userId": USER, "conversationId": "conversation-1"}]


@pytest.mark.asyncio
async def test_delete_returns_false_for_another_users_conversation() -> None:
    theirs = _to_document(_conversation(OTHER_USER))
    conversations = FakeCollection([theirs])
    repository = _repository(conversations=conversations)

    assert await repository.delete_conversation(USER, theirs["_id"]) is False
    # The other user's document survives.
    assert conversations.documents == [theirs]


@pytest.mark.asyncio
async def test_add_message_persists_scoped_document() -> None:
    messages = FakeCollection()
    repository = _repository(messages=messages)
    message = Message(
        user_id=USER,
        conversation_id="conversation-1",
        role=Role.USER,
        content="hello",
    )

    await repository.add_message(message)

    assert messages.documents[0]["userId"] == USER
    assert messages.documents[0]["_id"] == message.id
