"""Async downstream-call tests for the research agent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from research_agent.input import NormalizedInputItem
from research_agent.runtime import create_completion


class FakeResponsesClient:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.calls: list[dict[str, Any]] = []
        self.cancelled = asyncio.Event()

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return SimpleNamespace(output_text="bounded answer")


@pytest.mark.asyncio
async def test_forwards_ordered_roles_without_storage_and_bounds_output() -> None:
    client = FakeResponsesClient()
    input_items: list[NormalizedInputItem] = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "follow up"},
    ]

    result = await create_completion(
        client,
        model="model-deployment",
        input_items=input_items,
        max_output_tokens=321,
        cancellation_signal=asyncio.Event(),
        shutdown_signal=asyncio.Event(),
    )

    assert result == "bounded answer"
    assert client.calls[0]["input"] is input_items
    assert client.calls[0]["store"] is False
    assert client.calls[0]["max_output_tokens"] == 321


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_name", ["cancellation", "shutdown"])
async def test_cancels_the_live_sdk_request_when_a_signal_fires(signal_name: str) -> None:
    client = FakeResponsesClient(block=True)
    cancellation_signal = asyncio.Event()
    shutdown_signal = asyncio.Event()
    completion = asyncio.create_task(
        create_completion(
            client,
            model="model-deployment",
            input_items=[{"role": "user", "content": "question"}],
            max_output_tokens=321,
            cancellation_signal=cancellation_signal,
            shutdown_signal=shutdown_signal,
        )
    )
    await asyncio.sleep(0)

    if signal_name == "cancellation":
        cancellation_signal.set()
    else:
        shutdown_signal.set()

    with pytest.raises(asyncio.CancelledError):
        await completion
    assert client.cancelled.is_set()
