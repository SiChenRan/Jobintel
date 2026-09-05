"""JobIntel-owned provider contracts and construction."""

from jobintel.providers.base import (
    ContentBlock,
    LLMProvider,
    Message,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    TurnResult,
    Usage,
)
from jobintel.providers.factory import build_jobintel_provider

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
    "build_jobintel_provider",
]
