"""Provider construction and secret validation stay JobIntel-owned."""

from __future__ import annotations

import pytest

from jobintel.config import JobIntelProviderName, JobIntelSettings
from jobintel.providers.factory import build_jobintel_provider


@pytest.mark.parametrize(
    ("provider", "key_field", "expected_name"),
    [
        (JobIntelProviderName.ANTHROPIC, "anthropic_api_key", "anthropic"),
        (JobIntelProviderName.OPENAI, "openai_api_key", "openai"),
        (JobIntelProviderName.DEEPSEEK, "deepseek_api_key", "deepseek"),
    ],
)
def test_build_jobintel_provider_selects_owned_adapter(
    provider: JobIntelProviderName, key_field: str, expected_name: str
) -> None:
    settings = JobIntelSettings(
        _env_file=None,
        llm_provider=provider,
        **{key_field: "test-key"},
    )

    assert build_jobintel_provider(settings).name == expected_name


def test_selected_live_provider_requires_its_api_key() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_jobintel_provider(JobIntelSettings(_env_file=None, anthropic_api_key=None))


def test_require_keys_return_values_and_reject_missing() -> None:
    configured = JobIntelSettings(
        _env_file=None,
        anthropic_api_key="a",
        openai_api_key="o",
        deepseek_api_key="d",
    )
    assert configured.require_anthropic_key() == "a"
    assert configured.require_openai_key() == "o"
    assert configured.require_deepseek_key() == "d"

    missing = JobIntelSettings(
        _env_file=None,
        anthropic_api_key=None,
        openai_api_key=None,
        deepseek_api_key=None,
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        missing.require_openai_key()
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        missing.require_deepseek_key()
