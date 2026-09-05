"""Offline translation tests for every JobIntel-owned provider adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jobintel.config import JobIntelProviderName, JobIntelSettings
from jobintel.providers.anthropic_provider import AnthropicProvider
from jobintel.providers.base import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from jobintel.providers.deepseek_provider import DeepSeekProvider
from jobintel.providers.openai_provider import OpenAIProvider

_TOOLS = [
    ToolSpec(
        name="get_job",
        description="Fetch a job.",
        input_schema={"type": "object", "properties": {"job_id": {"type": "string"}}},
    )
]


def _conversation() -> list[Message]:
    return [
        Message.user_text("分析职位。"),
        Message(
            role="assistant",
            blocks=[
                TextBlock(text="正在读取。"),
                ToolUseBlock(id="tc1", name="get_job", input={"job_id": "J001"}),
            ],
        ),
        Message(
            role="user",
            blocks=[ToolResultBlock(tool_call_id="tc1", content="{}")],
        ),
    ]


class _FakeAnthropic:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="完成。"),
                SimpleNamespace(
                    type="tool_use", id="tc2", name="get_job", input={"job_id": "J001"}
                ),
            ],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


class _FakeOpenAI:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        tool_call = SimpleNamespace(
            id="tc9",
            function=SimpleNamespace(name="get_job", arguments='{"job_id":"J001"}'),
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="完成。", tool_calls=[tool_call]))
            ],
            usage=SimpleNamespace(prompt_tokens=13, completion_tokens=4),
        )


async def test_anthropic_translates_and_parses() -> None:
    client = _FakeAnthropic()
    provider = AnthropicProvider(
        JobIntelSettings(_env_file=None, anthropic_api_key="k"), client=client
    )

    result = await provider.run_turn("system", _conversation(), _TOOLS)

    assert result.text == "完成。"
    assert result.tool_calls[0].arguments == {"job_id": "J001"}
    assert result.usage.input_tokens == 11
    assert client.captured["messages"][-1]["content"][0]["type"] == "tool_result"


async def test_openai_translates_and_parses() -> None:
    client = _FakeOpenAI()
    provider = OpenAIProvider(JobIntelSettings(_env_file=None, openai_api_key="k"), client=client)

    result = await provider.run_turn("system", _conversation(), _TOOLS)

    assert result.tool_calls[0].arguments == {"job_id": "J001"}
    assert result.usage.output_tokens == 4
    assert "tool" in [message["role"] for message in client.captured["messages"]]


async def test_deepseek_disables_thinking_for_neutral_tool_replay() -> None:
    client = _FakeOpenAI()
    provider = DeepSeekProvider(
        JobIntelSettings(
            _env_file=None,
            llm_provider=JobIntelProviderName.DEEPSEEK,
            deepseek_api_key="k",
            deepseek_model="deepseek-test",
        ),
        client=client,
    )

    await provider.run_turn("system", _conversation(), _TOOLS)

    assert client.captured["model"] == "deepseek-test"
    assert client.captured["extra_body"] == {"thinking": {"type": "disabled"}}
