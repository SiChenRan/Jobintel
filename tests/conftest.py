"""Shared offline JobIntel test doubles."""

from __future__ import annotations

import pytest

from jobintel.providers.base import Message, ToolSpec, TurnResult, Usage


class FakeProvider:
    """Replay deterministic model turns without network access."""

    name = "fake"

    def __init__(self, turns: list[TurnResult]) -> None:
        """Initialize the scripted turn queue."""
        self._turns = list(turns)
        self.calls = 0
        self.received_messages: list[list[Message]] = []

    async def run_turn(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> TurnResult:
        """Return the next scripted turn and record the transcript."""
        del system, tools
        self.received_messages.append(list(messages))
        if self.calls >= len(self._turns):
            raise AssertionError("FakeProvider script exhausted")
        turn = self._turns[self.calls]
        self.calls += 1
        return turn


@pytest.fixture
def usage() -> Usage:
    """Return small reusable token accounting."""
    return Usage(input_tokens=100, output_tokens=50)
