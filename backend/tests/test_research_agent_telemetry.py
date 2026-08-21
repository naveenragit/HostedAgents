"""Token-usage span recording for the research agent.

The raw responses adapter emits no gen_ai telemetry, so these attributes are the
only token counts per-user cost reporting has for this agent. A silent regression
here shows up as an agent that appears free.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from research_agent.telemetry import record_usage


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def _response(**usage: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_123",
        model="gpt-5.4-2026-03-05",
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 100),
            output_tokens=usage.get("output_tokens", 20),
            input_tokens_details=SimpleNamespace(cached_tokens=usage.get("cached_tokens", 40)),
            output_tokens_details=SimpleNamespace(
                reasoning_tokens=usage.get("reasoning_tokens", 5)
            ),
        ),
    )


def test_records_every_billed_token_category_and_response_identity() -> None:
    span = FakeSpan()

    record_usage(span, _response())

    assert span.attributes == {
        "gen_ai.response.model": "gpt-5.4-2026-03-05",
        "gen_ai.response.id": "resp_123",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20,
        "gen_ai.usage.cache_read.input_tokens": 40,
        "gen_ai.usage.reasoning.output_tokens": 5,
    }


def test_records_zero_counts_rather_than_omitting_them() -> None:
    span = FakeSpan()

    record_usage(span, _response(cached_tokens=0, reasoning_tokens=0))

    # A zero is a measurement; omitting it would make the request look like it
    # predates cached-token reporting and skew cache-efficiency reporting.
    assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 0
    assert span.attributes["gen_ai.usage.reasoning.output_tokens"] == 0


def test_omits_absent_detail_sections_without_failing() -> None:
    span = FakeSpan()
    response = SimpleNamespace(
        id="resp_456",
        model="gpt-4o",
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )

    record_usage(span, response)

    assert span.attributes["gen_ai.usage.input_tokens"] == 7
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert "gen_ai.usage.cache_read.input_tokens" not in span.attributes


def test_ignores_malformed_counts_instead_of_reporting_them() -> None:
    span = FakeSpan()
    response = SimpleNamespace(
        id="",
        model=None,
        usage=SimpleNamespace(
            input_tokens="120",
            output_tokens=-1,
            input_tokens_details=SimpleNamespace(cached_tokens=None),
            output_tokens_details=SimpleNamespace(reasoning_tokens=True),
        ),
    )

    record_usage(span, response)

    # A string, a negative, a None and a bool are all unusable as token counts.
    # Recording them would put junk into a cost calculation.
    assert span.attributes == {}


def test_tolerates_a_response_with_no_usage_block() -> None:
    span = FakeSpan()

    record_usage(span, SimpleNamespace(id="resp_789", model="gpt-5.4"))

    assert span.attributes == {
        "gen_ai.response.model": "gpt-5.4",
        "gen_ai.response.id": "resp_789",
    }
