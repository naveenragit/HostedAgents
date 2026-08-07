"""Hosted-agent invocation contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services.agent_client import AgentClient, AgentInputItem


class _Responses:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            output_text="answer",
            id="response-1",
            output=[SimpleNamespace(id="assistant-message")],
        )


def _conversation_message(message_id: str, role: str, text: str) -> Any:
    content_type = "input_text" if role == "user" else "output_text"
    return SimpleNamespace(
        id=message_id,
        type="message",
        role=role,
        status="completed",
        content=[SimpleNamespace(type=content_type, text=text)],
    )


class _Items:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, dict[str, Any]]] = []
        self.retrieve_calls: list[tuple[str, str]] = []

    def list(self, conversation_id: str, **kwargs: Any) -> Any:
        self.list_calls.append((conversation_id, kwargs))
        return SimpleNamespace(
            data=[
                _conversation_message("assistant-message", "assistant", "answer"),
                _conversation_message("user-message", "user", "follow-up"),
            ],
            has_more=False,
            last_id="user-message",
        )

    def retrieve(self, item_id: str, *, conversation_id: str) -> Any:
        self.retrieve_calls.append((item_id, conversation_id))
        if item_id == "user-message":
            return _conversation_message(item_id, "user", "follow-up")
        return _conversation_message(item_id, "assistant", "answer")


class _Conversations:
    def __init__(self) -> None:
        self.create_calls = 0
        self.delete_calls: list[str] = []
        self.items = _Items()

    def create(self) -> Any:
        self.create_calls += 1
        return type("Conversation", (), {"id": "conversation-1"})()

    def delete(self, conversation_id: str) -> None:
        self.delete_calls.append(conversation_id)


class _OpenAIClient:
    def __init__(self) -> None:
        self.responses = _Responses()
        self.conversations = _Conversations()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProjectClient:
    def __init__(self) -> None:
        self.client = _OpenAIClient()
        self.kwargs: dict[str, Any] | None = None

    def get_openai_client(self, *, agent_name: str, **kwargs: Any) -> _OpenAIClient:
        assert agent_name == "research-agent"
        self.kwargs = kwargs
        return self.client


@pytest.mark.asyncio
async def test_send_without_conversation_preserves_roles_and_disables_storage() -> None:
    project = _ProjectClient()
    client = AgentClient(
        project_client=cast(Any, project),
        allowed_agents=("research-agent",),
        timeout_seconds=1,
    )
    input_items: list[AgentInputItem] = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "follow-up"},
    ]

    reply = await client.send("research-agent", input_items)

    assert project.client.responses.kwargs == {
        "input": input_items,
        "store": False,
    }
    assert project.kwargs == {"max_retries": 0, "timeout": 1}
    assert project.client.closed
    assert reply.text == "answer"
    assert reply.response_id == "response-1"


@pytest.mark.asyncio
async def test_create_and_send_with_foundry_conversation() -> None:
    project = _ProjectClient()
    client = AgentClient(
        project_client=cast(Any, project),
        allowed_agents=("research-agent",),
        timeout_seconds=1,
    )
    conversation_id = await client.create_conversation("research-agent")

    reply = await client.send(
        "research-agent",
        [{"role": "user", "content": "follow-up"}],
        conversation_id=conversation_id,
    )

    assert conversation_id == "conversation-1"
    assert project.client.conversations.create_calls == 1
    assert project.client.responses.kwargs == {
        "input": [{"role": "user", "content": "follow-up"}],
        "conversation": "conversation-1",
    }
    assert project.client.closed
    assert reply.text == "answer"
    assert reply.user_message_id == "user-message"
    assert reply.assistant_message_id == "assistant-message"


@pytest.mark.asyncio
async def test_list_retrieve_and_delete_foundry_conversation() -> None:
    project = _ProjectClient()
    client = AgentClient(
        project_client=cast(Any, project),
        allowed_agents=("research-agent",),
        timeout_seconds=1,
    )

    page = await client.list_conversation_messages(
        "research-agent",
        "conversation-1",
        limit=2,
        cursor=None,
    )
    replay = await client.get_conversation_messages(
        "research-agent",
        "conversation-1",
        "user-message",
        "assistant-message",
    )
    await client.delete_conversation("research-agent", "conversation-1")

    assert [(item.id, item.role, item.content) for item in page.items] == [
        ("user-message", "user", "follow-up"),
        ("assistant-message", "assistant", "answer"),
    ]
    assert page.next_cursor is None
    assert project.client.conversations.items.list_calls[-1] == (
        "conversation-1",
        {"order": "desc", "limit": 2},
    )
    assert [item.id for item in replay] == ["user-message", "assistant-message"]
    assert project.client.conversations.items.retrieve_calls == [
        ("user-message", "conversation-1"),
        ("assistant-message", "conversation-1"),
    ]
    assert project.client.conversations.delete_calls == ["conversation-1"]
