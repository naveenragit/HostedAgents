"""Mongo schema contracts without a database connection."""

from __future__ import annotations

from typing import Any

import pytest
from pymongo import ASCENDING

from app.repositories.mongo import MongoResources


class _Collection:
    def __init__(self) -> None:
        self.indexes: list[tuple[list[tuple[str, int]], dict[str, Any]]] = []

    async def create_index(
        self,
        keys: list[tuple[str, int]],
        **options: Any,
    ) -> None:
        self.indexes.append((keys, options))


class _Database:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.collections = {
            "conversations": _Collection(),
            "messages": _Collection(),
            "turns": _Collection(),
        }

    async def list_collection_names(self) -> list[str]:
        return []

    async def create_collection(self, name: str) -> None:
        self.created.append(name)

    def __getitem__(self, name: str) -> _Collection:
        return self.collections[name]


class _SchemaResources(MongoResources):
    def __init__(self, database: _Database) -> None:
        self._test_database = database
        self._conversations_collection = "conversations"
        self._messages_collection = "messages"
        self._turns_collection = "turns"

    @property
    def database(self) -> Any:
        return self._test_database


@pytest.mark.asyncio
async def test_schema_creates_turn_uniqueness_and_absolute_tombstone_ttl() -> None:
    database = _Database()
    resources = _SchemaResources(database)

    await resources.ensure_schema()

    assert database.created == ["conversations", "messages", "turns"]
    turn_indexes = database.collections["turns"].indexes
    assert (
        [
            ("userId", ASCENDING),
            ("conversationId", ASCENDING),
            ("idempotencyKey", ASCENDING),
        ],
        {"name": "user_conversation_idempotency", "unique": True},
    ) in turn_indexes
    assert (
        [("expiresAt", ASCENDING)],
        {"name": "deletion_tombstone_expiry", "expireAfterSeconds": 0},
    ) in database.collections["conversations"].indexes
