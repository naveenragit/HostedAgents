"""Agent Framework boundary middleware for deterministic, bounded triage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent_framework import AgentContext, AgentMiddleware, AgentResponse

from triage_agent.policy import TriagePolicyError, validate_response, validate_transcript
from triage_agent.tools import make_triage_decision

_TOOL_NAME = "triage_current_issue"


class TriageBoundaryMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Bind each invocation to one validated report and one canonical decision."""

    def __init__(self, max_input_chars: int) -> None:
        self._max_input_chars = max_input_chars

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if any(
            content.type != "text" for message in context.messages for content in message.contents
        ):
            raise TriagePolicyError("Triage requests must contain text only.")

        report = validate_transcript(
            [(message.role, message.text) for message in context.messages],
            max_input_chars=self._max_input_chars,
        )
        decision = make_triage_decision(report)

        def triage_current_issue() -> dict[str, str]:
            """Return the canonical triage decision for the current user report."""
            return decision.as_tool_result()

        context.tools = [triage_current_issue]
        context.options = {
            **dict(context.options or {}),
            "store": False,
            "tool_choice": {
                "mode": "required",
                "required_function_name": _TOOL_NAME,
            },
        }

        if context.stream:

            async def validate_final_response(response: AgentResponse) -> AgentResponse:
                validate_response(response.text, decision)
                return response

            context.stream_result_hooks.append(validate_final_response)
            await call_next()
            return

        await call_next()
        if not isinstance(context.result, AgentResponse):
            raise TriagePolicyError("The model did not return a complete triage response.")
        validate_response(context.result.text, decision)
