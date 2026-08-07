"""Conversation orchestration tests with no Azure or MongoDB dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.domain.models import Conversation, ConversationManager, Message, Role, Turn, TurnStatus
from app.repositories.mongo import TurnReservation
from app.services.agent_client import (
    AgentConversationMessage,
    AgentConversationPage,
    AgentInputItem,
    AgentReply,
)
from app.services.conversation_service import (
    AgentOutputTooLongError,
    ConversationService,
    InputTooLongError,
    build_agent_input,
)

USER = "user-alpha"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000001"
KEY = "00000000-0000-0000-0000-000000000002"
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _conversation(*, foundry_managed: bool = False) -> Conversation:
    return Conversation(
        id=CONVERSATION_ID,
        user_id=USER,
        agent_name="research-agent",
        conversation_manager=(
            ConversationManager.FOUNDRY if foundry_managed else ConversationManager.COSMOS
        ),
        foundry_conversation_id="foundry-conversation-1" if foundry_managed else None,
    )


def _message(message_id: str, role: Role, content: str) -> Message:
    return Message(
        id=message_id,
        user_id=USER,
        conversation_id=CONVERSATION_ID,
        role=role,
        content=content,
        created_at=NOW,
    )


def _turn() -> Turn:
    return Turn(
        id="turn-1",
        user_id=USER,
        conversation_id=CONVERSATION_ID,
        idempotency_key=KEY,
        content_hash="hash",
        status=TurnStatus.PENDING,
        attempt_id="attempt-1",
        lease_expires_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.conversation = _conversation()
        self.history: list[Message] = []
        self.reservation = TurnReservation(turn=_turn())
        self.created: Conversation | None = None
        self.completed: tuple[Message, Message] | None = None
        self.complete_kwargs: dict[str, Any] = {}
        self.delete_calls: list[bool] = []
        self.failed: tuple[str, str] | None = None
        self.indeterminate: tuple[str, str] | None = None

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        self.events.append("create")
        self.created = conversation
        self.conversation = conversation
        return conversation

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation | None:
        assert user_id == USER
        assert conversation_id == CONVERSATION_ID
        return self.conversation

    async def get_conversation_for_deletion(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Conversation | None:
        return await self.get_conversation(user_id, conversation_id)

    async def delete_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        purge: bool = True,
    ) -> bool:
        assert user_id == USER
        assert conversation_id == CONVERSATION_ID
        self.events.append("purge" if purge else "tombstone")
        self.delete_calls.append(purge)
        return True

    async def reserve_turn(self, *_args: Any, **_kwargs: Any) -> TurnReservation:
        self.events.append("reserve")
        return self.reservation

    async def list_recent_messages(self, *_args: Any, **_kwargs: Any) -> list[Message]:
        self.events.append("history")
        return self.history

    async def begin_turn_invocation(
        self,
        _user_id: str,
        _conversation_id: str,
        _turn_id: str,
        _attempt_id: str,
    ) -> None:
        self.events.append("begin")

    async def complete_turn(
        self,
        _user_id: str,
        _conversation_id: str,
        _turn_id: str,
        _attempt_id: str,
        user_message: Message,
        assistant_message: Message,
        **_kwargs: Any,
    ) -> None:
        self.events.append("complete")
        self.completed = (user_message, assistant_message)
        self.complete_kwargs = _kwargs

    async def fail_turn(
        self,
        _user_id: str,
        _conversation_id: str,
        turn_id: str,
        attempt_id: str,
        _failure_code: str,
    ) -> bool:
        self.events.append("fail")
        self.failed = (turn_id, attempt_id)
        return True

    async def mark_turn_indeterminate(
        self,
        _user_id: str,
        _conversation_id: str,
        turn_id: str,
        attempt_id: str,
        _failure_code: str,
    ) -> bool:
        self.events.append("indeterminate")
        self.indeterminate = (turn_id, attempt_id)
        return True


class FakeAgentClient:
    def __init__(self, *, reply: str = "answer", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[AgentInputItem]] = []
        self.conversation_ids: list[str | None] = []
        self.created_conversations: list[str] = []
        self.deleted_conversations: list[str] = []
        self.retrieved_turns: list[tuple[str, str, str]] = []
        self.message_page = AgentConversationPage(items=[], next_cursor=None)
        self.events: list[str] | None = None

    def ensure_known(self, agent_name: str) -> str:
        return agent_name

    async def create_conversation(self, agent_name: str) -> str:
        self.created_conversations.append(agent_name)
        return "foundry-conversation-1"

    async def delete_conversation(self, agent_name: str, conversation_id: str) -> None:
        assert agent_name == "research-agent"
        if self.events is not None:
            self.events.append("foundry-delete")
        self.deleted_conversations.append(conversation_id)

    async def list_conversation_messages(
        self,
        agent_name: str,
        conversation_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> AgentConversationPage:
        assert agent_name == "research-agent"
        assert conversation_id == "foundry-conversation-1"
        assert limit > 0
        assert cursor is None or cursor == "foundry-cursor"
        return self.message_page

    async def get_conversation_messages(
        self,
        agent_name: str,
        conversation_id: str,
        user_message_id: str,
        assistant_message_id: str,
    ) -> tuple[AgentConversationMessage, AgentConversationMessage]:
        assert agent_name == "research-agent"
        self.retrieved_turns.append((conversation_id, user_message_id, assistant_message_id))
        return (
            AgentConversationMessage(id=user_message_id, role="user", content="question"),
            AgentConversationMessage(
                id=assistant_message_id,
                role="assistant",
                content="answer",
            ),
        )

    async def send(
        self,
        *,
        agent_name: str,
        input_items: list[AgentInputItem],
        conversation_id: str | None = None,
    ) -> AgentReply:
        assert agent_name == "research-agent"
        if self.events is not None:
            self.events.append("agent")
        self.calls.append(input_items)
        self.conversation_ids.append(conversation_id)
        if self.error is not None:
            raise self.error
        return AgentReply(
            text=self.reply,
            response_id="response-1",
            user_message_id="foundry-user" if conversation_id is not None else None,
            assistant_message_id=("foundry-assistant" if conversation_id is not None else None),
        )


def _service(
    repository: FakeRepository,
    agents: FakeAgentClient,
    *,
    max_input_chars: int = 100,
    max_output_chars: int = 100,
    history_max_chars: int = 100,
    foundry_conversation_agents: tuple[str, ...] = (),
) -> ConversationService:
    return ConversationService(
        repository=cast(Any, repository),
        agent_client=cast(Any, agents),
        max_input_chars=max_input_chars,
        max_output_chars=max_output_chars,
        history_max_chars=history_max_chars,
        history_max_messages=20,
        turn_lease_seconds=180,
        foundry_conversation_agents=foundry_conversation_agents,
    )


def test_history_budget_selects_newest_complete_pairs_and_preserves_roles() -> None:
    history = [
        _message("orphan", Role.ASSISTANT, "boundary"),
        _message("1", Role.USER, "too-old-long"),
        _message("2", Role.ASSISTANT, "old"),
        _message("3", Role.USER, "four"),
        _message("4", Role.ASSISTANT, "five!"),
    ]

    items = build_agent_input(
        history,
        "new question",
        max_history_chars=9,
        max_total_chars=100,
    )

    assert items == [
        {"role": "user", "content": "four"},
        {"role": "assistant", "content": "five!"},
        {"role": "user", "content": "new question"},
    ]


def test_history_budget_omits_an_incomplete_trailing_user_message() -> None:
    history = [
        _message("1", Role.USER, "question"),
        _message("2", Role.ASSISTANT, "answer"),
        _message("3", Role.USER, "uncommitted"),
    ]

    items = build_agent_input(
        history,
        "new question",
        max_history_chars=100,
        max_total_chars=100,
    )

    assert items == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "new question"},
    ]


def test_total_input_budget_includes_history_and_current_message() -> None:
    history = [
        _message("1", Role.USER, "old question"),
        _message("2", Role.ASSISTANT, "old answer"),
        _message("3", Role.USER, "newer"),
        _message("4", Role.ASSISTANT, "reply"),
    ]

    items = build_agent_input(
        history,
        "current",
        max_history_chars=100,
        max_total_chars=17,
    )

    assert items == [
        {"role": "user", "content": "newer"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "current"},
    ]
    assert sum(len(item["content"]) for item in items) <= 17


@pytest.mark.asyncio
async def test_create_research_conversation_saves_foundry_link() -> None:
    repository = FakeRepository()
    agents = FakeAgentClient()
    service = _service(
        repository,
        agents,
        foundry_conversation_agents=("research-agent",),
    )

    conversation = await service.create_conversation(
        USER,
        "research-agent",
        "Research thread",
    )

    assert agents.created_conversations == ["research-agent"]
    assert repository.created == conversation
    assert conversation.user_id == USER
    assert conversation.conversation_manager is ConversationManager.FOUNDRY
    assert conversation.foundry_conversation_id == "foundry-conversation-1"


@pytest.mark.asyncio
async def test_foundry_send_uses_link_and_persists_only_item_references() -> None:
    repository = FakeRepository()
    repository.conversation = _conversation(foundry_managed=True)
    repository.history = [
        _message("old-user", Role.USER, "must not be loaded"),
        _message("old-assistant", Role.ASSISTANT, "Foundry owns this"),
    ]
    agents = FakeAgentClient()
    agents.events = repository.events
    service = _service(repository, agents)

    user_message, assistant_message = await service.send_message(
        USER,
        CONVERSATION_ID,
        "  follow-up  ",
        KEY,
    )

    assert repository.events == ["reserve", "begin", "agent", "complete"]
    assert agents.calls == [[{"role": "user", "content": "follow-up"}]]
    assert agents.conversation_ids == ["foundry-conversation-1"]
    assert user_message.id == "foundry-user"
    assert assistant_message.id == "foundry-assistant"
    assert user_message.created_at is None
    assert assistant_message.created_at is None
    assert repository.complete_kwargs["persist_messages"] is False
    assert repository.complete_kwargs["title"] is None


@pytest.mark.asyncio
async def test_completed_foundry_turn_replays_from_item_references() -> None:
    repository = FakeRepository()
    repository.conversation = _conversation(foundry_managed=True)
    repository.reservation = TurnReservation(
        turn=_turn().model_copy(
            update={
                "status": TurnStatus.COMPLETED,
                "user_message_id": "foundry-user",
                "assistant_message_id": "foundry-assistant",
            }
        )
    )
    agents = FakeAgentClient()
    service = _service(repository, agents)

    user_message, assistant_message = await service.send_message(
        USER,
        CONVERSATION_ID,
        "question",
        KEY,
    )

    assert repository.events == ["reserve"]
    assert agents.calls == []
    assert agents.retrieved_turns == [
        ("foundry-conversation-1", "foundry-user", "foundry-assistant")
    ]
    assert (user_message.id, assistant_message.id) == (
        "foundry-user",
        "foundry-assistant",
    )


@pytest.mark.asyncio
async def test_foundry_history_is_projected_without_cosmos_timestamps() -> None:
    repository = FakeRepository()
    repository.conversation = _conversation(foundry_managed=True)
    agents = FakeAgentClient()
    agents.message_page = AgentConversationPage(
        items=[
            AgentConversationMessage(id="foundry-user", role="user", content="question"),
            AgentConversationMessage(
                id="foundry-assistant",
                role="assistant",
                content="answer",
            ),
        ],
        next_cursor="foundry-cursor",
    )
    service = _service(repository, agents)

    page = await service.list_messages(
        USER,
        CONVERSATION_ID,
        limit=2,
        cursor=None,
    )

    assert [message.id for message in page.items] == [
        "foundry-user",
        "foundry-assistant",
    ]
    assert all(message.created_at is None for message in page.items)
    assert page.next_cursor == "foundry-cursor"


@pytest.mark.asyncio
async def test_foundry_delete_fences_cosmos_before_remote_cleanup() -> None:
    repository = FakeRepository()
    repository.conversation = _conversation(foundry_managed=True)
    agents = FakeAgentClient()
    agents.events = repository.events
    service = _service(repository, agents)

    await service.delete_conversation(USER, CONVERSATION_ID)

    assert repository.events == ["tombstone", "foundry-delete", "purge"]
    assert repository.delete_calls == [False, True]
    assert agents.deleted_conversations == ["foundry-conversation-1"]


@pytest.mark.asyncio
async def test_send_reserves_then_invokes_stateless_agent_then_commits() -> None:
    repository = FakeRepository()
    repository.history = [
        _message("1", Role.USER, "earlier question"),
        _message("2", Role.ASSISTANT, "earlier answer"),
    ]
    agents = FakeAgentClient()
    agents.events = repository.events
    service = _service(repository, agents)

    user_message, assistant_message = await service.send_message(
        USER,
        CONVERSATION_ID,
        "  follow-up  ",
        KEY,
    )

    assert repository.events == ["reserve", "history", "begin", "agent", "complete"]
    assert agents.calls == [
        [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "follow-up"},
        ]
    ]
    assert user_message.content == "follow-up"
    assert assistant_message.content == "answer"
    assert repository.completed == (user_message, assistant_message)


@pytest.mark.asyncio
async def test_completed_idempotent_turn_replays_without_agent_call() -> None:
    repository = FakeRepository()
    replay_user = _message("user-message", Role.USER, "question")
    replay_assistant = _message("assistant-message", Role.ASSISTANT, "answer")
    repository.reservation = TurnReservation(
        turn=_turn().model_copy(update={"status": TurnStatus.COMPLETED}),
        user_message=replay_user,
        assistant_message=replay_assistant,
    )
    agents = FakeAgentClient()
    service = _service(repository, agents)

    result = await service.send_message(USER, CONVERSATION_ID, "question", KEY)

    assert result == (replay_user, replay_assistant)
    assert repository.events == ["reserve"]
    assert agents.calls == []


@pytest.mark.asyncio
async def test_agent_failure_marks_dispatched_turn_indeterminate() -> None:
    repository = FakeRepository()
    agents = FakeAgentClient(error=RuntimeError("model unavailable"))
    agents.events = repository.events
    service = _service(repository, agents)

    with pytest.raises(RuntimeError, match="outcome could not be confirmed"):
        await service.send_message(USER, CONVERSATION_ID, "question", KEY)

    assert repository.events == ["reserve", "history", "begin", "agent", "indeterminate"]
    assert repository.completed is None
    assert repository.failed is None
    assert repository.indeterminate == ("turn-1", "attempt-1")


@pytest.mark.asyncio
async def test_input_over_config_limit_is_rejected_not_truncated() -> None:
    repository = FakeRepository()
    agents = FakeAgentClient()
    service = _service(repository, agents, max_input_chars=4)

    with pytest.raises(InputTooLongError):
        await service.send_message(USER, CONVERSATION_ID, "12345", KEY)

    assert repository.events == []
    assert agents.calls == []


@pytest.mark.asyncio
async def test_output_over_config_limit_is_not_persisted() -> None:
    repository = FakeRepository()
    agents = FakeAgentClient(reply="12345")
    service = _service(repository, agents, max_output_chars=4)

    with pytest.raises(AgentOutputTooLongError):
        await service.send_message(USER, CONVERSATION_ID, "question", KEY)

    assert repository.completed is None
    assert repository.failed is None
    assert repository.indeterminate == ("turn-1", "attempt-1")
