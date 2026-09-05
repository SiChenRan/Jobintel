"""Anthropic Messages API adapter for the JobIntel LLM protocol."""

from __future__ import annotations

from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam

from jobintel.config import JobIntelSettings
from jobintel.providers.base import (
    Message,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    TurnResult,
    Usage,
)

_MAX_TOKENS = 4096


class AnthropicProvider:
    """Run neutral tool-use turns against the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, settings: JobIntelSettings, client: AsyncAnthropic | None = None) -> None:
        """Configure the selected model and an optional injected client."""
        self._model = settings.anthropic_model
        self._client = client or AsyncAnthropic(api_key=settings.require_anthropic_key())

    async def run_turn(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> TurnResult:
        """Translate, execute, and normalize one Anthropic turn."""
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[_to_anthropic_message(message) for message in messages],
            tools=[_to_anthropic_tool(tool) for tool in tools],
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if isinstance(block.input, dict) else {},
                    )
                )
        return TurnResult(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )


def _to_anthropic_tool(spec: ToolSpec) -> ToolParam:
    """Translate one neutral tool spec."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }


def _to_anthropic_message(message: Message) -> MessageParam:
    """Translate one neutral transcript message."""
    content: list[dict[str, Any]] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_call_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
    return cast(MessageParam, {"role": message.role, "content": content})
