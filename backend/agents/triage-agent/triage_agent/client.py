"""Owned Foundry client resources for the hosted triage agent."""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import TracebackType
from typing import Self

from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import DefaultAzureCredential


class ManagedFoundryChatClient(FoundryChatClient):  # type: ignore[misc]
    """Close transports and credentials created by the preview Foundry client."""

    def __init__(
        self,
        *,
        project_endpoint: str,
        model: str,
        credential: DefaultAzureCredential,
    ) -> None:
        super().__init__(
            project_endpoint=project_endpoint,
            model=model,
            credential=credential,
            function_invocation_configuration={
                "max_iterations": 2,
                "max_function_calls": 1,
                "terminate_on_unknown_calls": True,
            },
        )
        self._credential = credential
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close every resource owned by this client exactly once."""
        if self._closed:
            return
        self._closed = True

        stack = AsyncExitStack()
        stack.push_async_callback(self._credential.close)
        stack.push_async_callback(self.project_client.close)
        stack.push_async_callback(self.client.close)
        await stack.aclose()
