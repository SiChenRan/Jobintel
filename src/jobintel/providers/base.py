"""Provider-neutral, single-turn LLM protocol owned by JobIntel."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ToolSpec(BaseModel):
    """A tool advertised to the model."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(description="JSON Schema for tool arguments.")


class ToolCall(BaseModel):
    """A model request to invoke one tool."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TextBlock(BaseModel):
    """A plain-text content block."""

    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """An assistant block requesting a tool call."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """A user block returning one tool-call result."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    """One neutral conversation turn."""

    role: Literal["user", "assistant"]
    blocks: list[ContentBlock]

    @classmethod
    def user_text(cls, text: str) -> Message:
        """Build a user message containing one text block."""
        return cls(role="user", blocks=[TextBlock(text=text)])


class Usage(BaseModel):
    """Token accounting for one or more turns."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        """Return combined token usage."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class TurnResult(BaseModel):
    """The normalized outcome of a single model turn."""

    text: str | None = Field(default=None, description="Free text emitted by the model.")
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        """Return whether the model requested at least one tool call."""
        return bool(self.tool_calls)

    def assistant_message(self) -> Message:
        """Reconstruct the assistant message for transcript replay."""
        blocks: list[ContentBlock] = []
        if self.text:
            blocks.append(TextBlock(text=self.text))
        blocks.extend(
            ToolUseBlock(id=call.id, name=call.name, input=call.arguments)
            for call in self.tool_calls
        )
        return Message(role="assistant", blocks=blocks)


@runtime_checkable
class LLMProvider(Protocol):
    """The sole LLM boundary used by JobIntel orchestration."""

    name: str

    async def run_turn(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> TurnResult:
        """Run one stateless model turn against the complete transcript."""
        ...


__all__ = [
    "ContentBlock",
    "LLMProvider",
    "Message",
    "TextBlock",
    "ToolCall",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "TurnResult",
    "Usage",
]
