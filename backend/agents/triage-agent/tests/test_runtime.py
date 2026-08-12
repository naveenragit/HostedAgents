"""Offline contract tests against the pinned Agent Framework runtime."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Self, cast
from unittest.mock import patch

from agent_framework import Agent, AgentContext, AgentResponse, Message
from agent_framework_foundry_hosting import ResponsesHostServer

import main as composition_root
from triage_agent.client import ManagedFoundryChatClient
from triage_agent.config import AgentConfig
from triage_agent.middleware import GatewayAttributionMiddleware, TriageBoundaryMiddleware
from triage_agent.policy import TriagePolicyError
from triage_agent.tools import make_triage_decision


class _AsyncCloseable:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _LifecycleClient(AbstractAsyncContextManager["_LifecycleClient"]):
    def __init__(self) -> None:
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self) -> Self:
        self.enter_calls += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_calls += 1


class TriageMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_streaming_request_binds_tool_and_validates_response(self) -> None:
        report = "Production is down and users cannot sign in"
        decision = make_triage_decision(report)
        context = AgentContext(
            agent=cast(Any, object()),
            messages=[Message("user", [report])],
            options={"store": True, "tool_choice": "none"},
        )

        async def call_next() -> None:
            context.result = AgentResponse(
                messages=[Message("assistant", [decision.as_response()])]
            )

        await TriageBoundaryMiddleware(max_input_chars=1_000).process(context, call_next)

        tools = cast(list[Callable[[], dict[str, str]]], context.tools)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].__name__, "triage_current_issue")
        self.assertEqual(tools[0](), decision.as_tool_result())
        self.assertEqual(context.options["store"], False)
        self.assertEqual(
            context.options["tool_choice"],
            {"mode": "required", "required_function_name": "triage_current_issue"},
        )

    async def test_input_limit_rejects_before_model_invocation(self) -> None:
        called = False
        context = AgentContext(
            agent=cast(Any, object()),
            messages=[Message("user", ["123456"])],
        )

        async def call_next() -> None:
            nonlocal called
            called = True

        with self.assertRaisesRegex(TriagePolicyError, "configured 5-character limit"):
            await TriageBoundaryMiddleware(max_input_chars=5).process(context, call_next)
        self.assertFalse(called)

    async def test_streaming_request_registers_final_response_validator(self) -> None:
        report = "The export button is broken in Safari"
        decision = make_triage_decision(report)
        context = AgentContext(
            agent=cast(Any, object()),
            messages=[Message("user", [report])],
            stream=True,
        )

        async def call_next() -> None:
            return None

        await TriageBoundaryMiddleware(max_input_chars=1_000).process(context, call_next)

        self.assertEqual(len(context.stream_result_hooks), 1)
        hook = cast(
            Callable[[AgentResponse], Awaitable[AgentResponse]],
            context.stream_result_hooks[0],
        )
        valid_response = AgentResponse(messages=[Message("assistant", [decision.as_response()])])
        self.assertIs(await hook(valid_response), valid_response)
        with self.assertRaisesRegex(TriagePolicyError, "deterministic triage decision"):
            await hook(AgentResponse(messages=[Message("assistant", ["invalid"])]))


class TriageConstructionTests(unittest.IsolatedAsyncioTestCase):
    def test_build_agent_preserves_stateless_and_bounded_configuration(self) -> None:
        credential = object()
        client = object()
        config = AgentConfig(
            project_endpoint="https://example.invalid/api/projects/test",
            model_deployment_name="test-model",
            log_level="INFO",
            max_input_chars=321,
            max_output_tokens=456,
            request_timeout_seconds=60.0,
            model_gateway_url=None,
        )

        with (
            patch.object(composition_root, "DefaultAzureCredential", return_value=credential),
            patch.object(
                composition_root,
                "ManagedFoundryChatClient",
                return_value=client,
            ) as client_type,
        ):
            agent = composition_root.build_agent(config)

        client_type.assert_called_once_with(
            project_endpoint=config.project_endpoint,
            model=config.model_deployment_name,
            credential=credential,
        )
        self.assertIs(agent.client, client)
        self.assertEqual(agent.default_options["store"], False)
        self.assertEqual(agent.default_options["max_tokens"], 456)
        self.assertEqual(agent.default_options["tools"], [])
        self.assertEqual(len(agent.middleware or []), 1)
        self.assertIsInstance((agent.middleware or [None])[0], TriageBoundaryMiddleware)

    def test_build_agent_routes_through_gateway_and_attributes_the_user(self) -> None:
        credential = object()
        client = object()
        config = AgentConfig(
            project_endpoint="https://example.invalid/api/projects/test",
            model_deployment_name="test-model",
            log_level="INFO",
            max_input_chars=321,
            max_output_tokens=456,
            request_timeout_seconds=45.0,
            model_gateway_url="https://gateway.invalid/agents/openai/v1",
        )

        with (
            patch.object(composition_root, "DefaultAzureCredential", return_value=credential),
            patch.object(
                composition_root,
                "create_gateway_chat_client",
                return_value=client,
            ) as factory,
            patch.object(composition_root, "ManagedFoundryChatClient") as direct_client_type,
        ):
            agent = composition_root.build_agent(config)

        factory.assert_called_once_with(
            base_url=config.model_gateway_url,
            model=config.model_deployment_name,
            timeout_seconds=45.0,
        )
        direct_client_type.assert_not_called()
        self.assertIs(agent.client, client)
        middleware = list(agent.middleware or [])
        self.assertEqual(len(middleware), 2)
        # Attribution must run before the triage boundary so every downstream
        # model call carries the gateway headers.
        self.assertIsInstance(middleware[0], GatewayAttributionMiddleware)
        self.assertIsInstance(middleware[1], TriageBoundaryMiddleware)

    async def test_managed_client_closes_each_owned_resource_once(self) -> None:
        openai_client = _AsyncCloseable()
        project_client = _AsyncCloseable()
        credential = _AsyncCloseable()
        client = object.__new__(ManagedFoundryChatClient)
        client.client = cast(Any, openai_client)
        client.project_client = cast(Any, project_client)
        client._credential = cast(Any, credential)
        client._closed = False

        await client.close()
        await client.close()

        self.assertEqual(openai_client.close_calls, 1)
        self.assertEqual(project_client.close_calls, 1)
        self.assertEqual(credential.close_calls, 1)

    async def test_responses_host_enters_and_closes_agent_resources(self) -> None:
        client = _LifecycleClient()
        agent = Agent(client=cast(Any, client), default_options={"store": False})
        with patch("socket.socket.connect", side_effect=AssertionError("network access attempted")):
            host = ResponsesHostServer(agent, configure_observability=None)
            await host._ensure_agent_ready()
            await host._ensure_agent_ready()
            await host._cleanup_agent()
            await host._cleanup_agent()

        self.assertEqual(client.enter_calls, 1)
        self.assertEqual(client.exit_calls, 1)


if __name__ == "__main__":
    unittest.main()
