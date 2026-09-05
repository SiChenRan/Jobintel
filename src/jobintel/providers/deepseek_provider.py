"""DeepSeek adapter through its OpenAI-compatible API."""

from __future__ import annotations

from typing import Any

from jobintel.config import JobIntelSettings
from jobintel.providers.openai_provider import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """Use DeepSeek credentials while preserving the neutral tool protocol."""

    name = "deepseek"

    def __init__(self, settings: JobIntelSettings, client: Any | None = None) -> None:
        """Construct a DeepSeek client or accept an injected test client."""
        self._model = settings.deepseek_model
        self._request_options: dict[str, Any] = {"extra_body": {"thinking": {"type": "disabled"}}}
        if client is not None:
            self._client = client
            return
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=settings.require_deepseek_key(),
            base_url=settings.deepseek_base_url,
        )
