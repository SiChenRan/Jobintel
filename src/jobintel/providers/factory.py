"""Construct JobIntel-owned LLM provider adapters."""

from __future__ import annotations

from jobintel.config import JobIntelProviderName, JobIntelSettings
from jobintel.providers.base import LLMProvider


def build_jobintel_provider(settings: JobIntelSettings) -> LLMProvider:
    """Build the live provider selected by JobIntel settings."""
    if settings.llm_provider is JobIntelProviderName.ANTHROPIC:
        from jobintel.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if settings.llm_provider is JobIntelProviderName.DEEPSEEK:
        from jobintel.providers.deepseek_provider import DeepSeekProvider

        return DeepSeekProvider(settings)
    from jobintel.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(settings)
