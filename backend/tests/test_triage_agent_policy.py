"""Tests for the pure triage request and response boundary policy."""

from __future__ import annotations

import pytest

from triage_agent.policy import TriagePolicyError, validate_response, validate_transcript
from triage_agent.tools import make_triage_decision


def test_transcript_returns_latest_non_empty_user_report() -> None:
    messages = [
        ("user", "First report"),
        ("assistant", "Prior answer"),
        ("user", "  Current report  "),
    ]

    assert validate_transcript(messages, max_input_chars=100) == "Current report"


def test_transcript_limit_is_aggregate() -> None:
    messages = [("user", "12345"), ("assistant", "67890"), ("user", "x")]

    with pytest.raises(TriagePolicyError, match="configured 10-character limit"):
        validate_transcript(messages, max_input_chars=10)


@pytest.mark.parametrize("messages", [[], [("assistant", "answer")], [("user", "   ")]])
def test_transcript_requires_non_empty_user_report(messages: list[tuple[str, str]]) -> None:
    with pytest.raises(TriagePolicyError, match="non-empty user report"):
        validate_transcript(messages, max_input_chars=100)


def test_response_must_exactly_match_canonical_decision() -> None:
    decision = make_triage_decision("The checkout payment is failing")

    validate_response(decision.as_response(), decision)
    with pytest.raises(TriagePolicyError, match="deterministic triage decision"):
        validate_response(f"{decision.as_response()}\nExtra text", decision)
