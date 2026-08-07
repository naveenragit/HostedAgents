"""Pure request and response policy for the triage agent boundary."""

from __future__ import annotations

from collections.abc import Sequence

from triage_agent.tools import TriageDecision


class TriagePolicyError(ValueError):
    """Raised when a request or model response violates the triage contract."""


def validate_transcript(
    messages: Sequence[tuple[str, str]],
    *,
    max_input_chars: int,
) -> str:
    """Validate aggregate text input and return the latest user report."""
    total_chars = sum(len(text) for _, text in messages)
    if total_chars > max_input_chars:
        raise TriagePolicyError(
            f"Request text exceeds the configured {max_input_chars}-character limit."
        )

    for role, text in reversed(messages):
        if role == "user" and text.strip():
            return text.strip()

    raise TriagePolicyError("A non-empty user report is required.")


def validate_response(response: str, expected: TriageDecision) -> None:
    """Require the exact canonical response without exposing bad output in errors."""
    if response != expected.as_response():
        raise TriagePolicyError(
            "The model response did not match the deterministic triage decision."
        )
