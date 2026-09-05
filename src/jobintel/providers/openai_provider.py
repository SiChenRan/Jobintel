"""OpenAI-compatible Chat Completions adapter for JobIntel."""

from __future__ import annotations

import json
from typing import Any

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


class OpenAIProvider:
    """Run neutral tool-use turns against an OpenAI-compatible API."""

    name = "openai"

    def __init__(self, settings: JobIntelSettings, client: Any | None = None) -> None:
        """Configure the selected model and an optional injected client."""
        self._model = settings.openai_model
        self._request_options: dict[str, Any] = {}
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=settings.require_openai_key())

    async def run_turn(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> TurnResult:
        """Translate, execute, and normalize one compatible model turn."""
        payload: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            payload.extend(_to_openai_messages(message))
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=payload,
            tools=[_to_openai_tool(tool) for tool in tools],
            tool_choice="auto",
            **self._request_options,
        )
        choice = response.choices[0].message
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_parse_args(call.function.arguments),
            )
            for call in choice.tool_calls or []
        ]
        usage = response.usage
        return TurnResult(
            text=choice.content,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )


def _parse_args(raw: str | None) -> dict[str, Any]:
    """Parse one JSON tool-argument string, tolerating empties."""
    if not raw:
        return {}
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    """Translate one neutral tool spec."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


def _to_openai_messages(message: Message) -> list[dict[str, Any]]:
    """Translate one neutral message into compatible API messages."""
    if message.role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        return [assistant]

    translated: list[dict[str, Any]] = []
    text_parts = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            translated.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_call_id,
                    "content": block.content,
                }
            )
    if text_parts:
        translated.append({"role": "user", "content": "\n".join(text_parts)})
    return translated
