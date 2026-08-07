"""Cancellable downstream Responses invocation for the research agent."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from research_agent.input import NormalizedInputItem
from research_agent.instructions import SYSTEM_INSTRUCTIONS


async def create_completion(
    responses_client: Any,
    *,
    model: str,
    input_items: list[NormalizedInputItem],
    max_output_tokens: int,
    cancellation_signal: asyncio.Event,
    shutdown_signal: asyncio.Event,
) -> str:
    """Invoke Responses and cancel the live SDK request when either signal fires."""
    request_task = asyncio.ensure_future(
        responses_client.create(
            model=model,
            instructions=SYSTEM_INSTRUCTIONS,
            input=input_items,
            max_output_tokens=max_output_tokens,
            store=False,
        )
    )
    cancellation_waiter = asyncio.create_task(cancellation_signal.wait())
    shutdown_waiter = asyncio.create_task(shutdown_signal.wait())
    signal_waiters = (cancellation_waiter, shutdown_waiter)

    try:
        completed, _ = await asyncio.wait(
            {request_task, *signal_waiters},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if request_task in completed:
            response = await request_task
            return str(response.output_text)

        request_task.cancel()
        with suppress(asyncio.CancelledError):
            await request_task
        raise asyncio.CancelledError
    finally:
        for waiter in signal_waiters:
            waiter.cancel()
        await asyncio.gather(*signal_waiters, return_exceptions=True)
