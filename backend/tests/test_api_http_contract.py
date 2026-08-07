"""HTTP boundary contracts with no MongoDB, Azure, or model access."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.dependencies import get_conversation_service
from app.domain.models import Conversation, ConversationManager, Message, Role
from app.services.agent_client import AgentInvocationError
from app.services.conversation_service import (
    ConversationNotFoundError,
    InputTooLongError,
    TurnOutcomeUnknownError,
    TurnPendingError,
)

_CONVERSATION_ID = "00000000-0000-0000-0000-000000000001"
_POC_USER_ID = "poc-test-user"
_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class _Service:
    def __init__(self) -> None:
        self.seen_user_ids: list[str] = []
        self.send_error: Exception | None = None

    async def get_conversation(self, user_id: str, conversation_id: str) -> Conversation:
        self.seen_user_ids.append(user_id)
        if user_id != _POC_USER_ID or conversation_id != _CONVERSATION_ID:
            raise ConversationNotFoundError(conversation_id)
        return Conversation(
            id=conversation_id,
            user_id=user_id,
            agent_name="research-agent",
            created_at=_NOW,
            updated_at=_NOW,
            conversation_manager=ConversationManager.FOUNDRY,
            foundry_conversation_id="server-only-foundry-id",
        )

    async def send_message(
        self,
        *,
        user_id: str,
        conversation_id: str,
        content: str,
        idempotency_key: str,
    ) -> tuple[Message, Message]:
        self.seen_user_ids.append(user_id)
        if self.send_error is not None:
            raise self.send_error
        return (
            Message(
                id="user-message",
                user_id=user_id,
                conversation_id=conversation_id,
                role=Role.USER,
                content=content,
                created_at=_NOW,
            ),
            Message(
                id="assistant-message",
                user_id=user_id,
                conversation_id=conversation_id,
                role=Role.ASSISTANT,
                content="reply",
                created_at=_NOW,
            ),
        )


def _create_test_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _Service]:
    values = {
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://example-account.services.ai.azure.com/api/projects/example-project"
        ),
        "AGENT_NAMES": "research-agent,triage-agent",
        "MONGO_CONNECTION_STRING": (
            "mongodb+srv://cluster.mongocluster.cosmos.azure.com/?authMechanism=MONGODB-OIDC"
        ),
        "API_POC_MODE": "true",
        "API_POC_USER_ID": _POC_USER_ID,
        "AZURE_CREDENTIAL_MODE": "local",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    from app.main import create_app

    app = create_app()
    service = _Service()
    app.dependency_overrides[get_conversation_service] = lambda: service
    return app, service


@pytest_asyncio.fixture
async def poc_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, _Service]]:
    app, service = _create_test_app(monkeypatch)
    transport = ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client, service
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_openapi_declares_pagination_and_required_idempotency_key(
    poc_client: tuple[AsyncClient, _Service],
) -> None:
    client, _service = poc_client

    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    list_parameters = schema["paths"]["/api/conversations"]["get"]["parameters"]
    message_parameters = schema["paths"]["/api/conversations/{conversation_id}/messages"]
    transcript_parameters = message_parameters["get"]["parameters"]
    send_parameters = message_parameters["post"]["parameters"]

    assert {(item["name"], item["in"]) for item in list_parameters} >= {
        ("limit", "query"),
        ("cursor", "query"),
    }
    assert {(item["name"], item["in"]) for item in transcript_parameters} >= {
        ("limit", "query"),
        ("cursor", "query"),
    }
    idempotency = next(item for item in send_parameters if item["name"] == "Idempotency-Key")
    assert idempotency["in"] == "header"
    assert idempotency["required"] is True
    conversation_properties = schema["components"]["schemas"]["ConversationResponse"]["properties"]
    assert "conversationManager" in conversation_properties
    assert "foundryConversationId" not in conversation_properties
    assert schema.get("components", {}).get("securitySchemes") in (None, {})


@pytest.mark.asyncio
async def test_loopback_request_needs_no_credentials_and_uses_fixed_identity(
    poc_client: tuple[AsyncClient, _Service],
) -> None:
    client, service = poc_client

    response = await client.get(
        f"/api/conversations/{_CONVERSATION_ID}",
        headers={
            "Authorization": "Bearer ignored",
            "x-dev-user-id": "attacker-selected-user",
        },
    )

    assert response.status_code == 200
    assert service.seen_user_ids == [_POC_USER_ID]
    assert response.json()["conversationManager"] == "foundry"
    assert "foundryConversationId" not in response.json()


@pytest.mark.asyncio
async def test_remote_peer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _service = _create_test_app(monkeypatch)
    transport = ASGITransport(app=app, client=("203.0.113.10", 50000))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/api/capabilities")
    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 403
    assert response.json() == {
        "detail": "The unauthenticated POC API is available only over loopback."
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/capabilities", "/health/live", "/openapi.json"])
async def test_non_loopback_host_is_rejected_before_routing(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    app, _service = _create_test_app(monkeypatch)
    transport = ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with AsyncClient(transport=transport, base_url="http://attacker.example") as client:
        response = await client.get(path)
    app.dependency_overrides.clear()
    get_settings.cache_clear()

    assert response.status_code == 403
    assert response.json() == {"detail": "The POC API requires a loopback Host header."}


@pytest.mark.asyncio
async def test_missing_idempotency_key_is_rejected_before_dispatch(
    poc_client: tuple[AsyncClient, _Service],
) -> None:
    client, service = poc_client

    response = await client.post(
        f"/api/conversations/{_CONVERSATION_ID}/messages",
        json={"content": "hello"},
    )

    assert response.status_code == 422
    assert service.seen_user_ids == []


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail", "retry_after"),
    [
        (TurnPendingError(7), 409, "Turn is already pending.", "7"),
        (
            TurnOutcomeUnknownError("The turn outcome is unknown and cannot be retried."),
            409,
            "The turn outcome is unknown and cannot be retried.",
            None,
        ),
        (InputTooLongError(100), 413, "Message content must not exceed 100 characters.", None),
        (
            AgentInvocationError("private upstream detail"),
            502,
            "The agent is currently unavailable. Please retry.",
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_error_mappings(
    poc_client: tuple[AsyncClient, _Service],
    error: Exception,
    expected_status: int,
    expected_detail: str,
    retry_after: str | None,
) -> None:
    client, service = poc_client
    service.send_error = error

    response = await client.post(
        f"/api/conversations/{_CONVERSATION_ID}/messages",
        headers={"Idempotency-Key": "request-1"},
        json={"content": "hello"},
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert response.headers.get("retry-after") == retry_after
