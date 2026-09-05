"""Compatibility tests for the provider-neutral protocol reused by JobIntel."""

from __future__ import annotations

from jobintel.providers.base import Message, ToolCall, TurnResult, Usage


def test_jobintel_reuses_provider_neutral_message_contract() -> None:
    turn = TurnResult(
        text="Inspecting evidence",
        tool_calls=[ToolCall(id="call-1", name="get_job", arguments={"job_id": "J001"})],
        usage=Usage(input_tokens=10, output_tokens=4),
    )

    message = turn.assistant_message()

    assert isinstance(message, Message)
    assert turn.wants_tools is True
    assert turn.usage.input_tokens == 10
