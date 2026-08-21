"""Logging and model-call telemetry setup.

Deliberately PII-safe: we log sizes, identifiers and outcomes, never message
bodies. Azure SDK HTTP body/header logging is left disabled — enabling it would
write prompts and completions into Application Insights.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final, Protocol

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_TRACER: Final = trace.get_tracer("research_agent")


class SupportsSetAttribute(Protocol):
    """The only span capability usage recording needs."""

    def set_attribute(self, key: str, value: Any) -> None: ...


def configure_logging(level: str) -> logging.Logger:
    """Configure root logging and return the agent logger."""
    resolved = getattr(logging, level, logging.INFO)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    # Azure SDK loggers are chatty and can echo request/response detail.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

    return logging.getLogger("research_agent")


@contextmanager
def model_call_span(model: str) -> Iterator[Span]:
    """Open a gen_ai client span covering one downstream model call.

    The raw responses adapter emits no gen_ai telemetry of its own, so per-user
    cost reporting has no token counts for this agent unless it records them
    here. Attribute names follow the OpenTelemetry gen_ai conventions that the
    Foundry runtime already emits for Agent-Framework agents, so a single query
    prices both agents.
    """
    with _TRACER.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "openai")
        span.set_attribute("gen_ai.request.model", model)
        yield span


def record_usage(span: SupportsSetAttribute, response: Any) -> None:
    """Copy token counts and response identity off a Responses result onto ``span``.

    Cached and reasoning counts are recorded separately because they are billed
    on their own meters; folding them into the input and output totals would
    misprice every request that uses them.
    """
    model = _text(response, "model")
    if model is not None:
        span.set_attribute("gen_ai.response.model", model)
    response_id = _text(response, "id")
    if response_id is not None:
        span.set_attribute("gen_ai.response.id", response_id)

    usage = getattr(response, "usage", None)
    if usage is None:
        return

    counts = {
        "gen_ai.usage.input_tokens": _count(usage, "input_tokens"),
        "gen_ai.usage.output_tokens": _count(usage, "output_tokens"),
        "gen_ai.usage.cache_read.input_tokens": _count(
            getattr(usage, "input_tokens_details", None), "cached_tokens"
        ),
        "gen_ai.usage.reasoning.output_tokens": _count(
            getattr(usage, "output_tokens_details", None), "reasoning_tokens"
        ),
    }
    for attribute, value in counts.items():
        if value is not None:
            span.set_attribute(attribute, value)


def _count(source: object, name: str) -> int | None:
    """Read a non-negative integer token count, or None when absent or malformed."""
    value = getattr(source, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(source: object, name: str) -> str | None:
    value = getattr(source, name, None)
    return value if isinstance(value, str) and value else None
