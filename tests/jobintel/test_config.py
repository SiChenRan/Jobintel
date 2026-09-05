"""Tests for the independent JobIntel database setting."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jobintel.config import (
    JobIntelProviderName,
    JobIntelSettings,
    load_jobintel_settings,
)


def test_jobintel_defaults() -> None:
    settings = JobIntelSettings(_env_file=None)
    assert settings.jobintel_db_path == Path("data/jobintel.db")
    assert settings.agent_max_iterations == 12
    assert settings.agent_max_repairs == 2
    assert settings.agent_max_tool_calls == 60
    assert settings.parser_max_repairs == 2
    assert settings.outreach_max_repairs == 2
    assert settings.smtp_notification_ready is False
    assert settings.smtp_port == 587
    assert settings.llm_provider is JobIntelProviderName.ANTHROPIC
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.discovery_search_min_delay_seconds == 1.2
    assert settings.discovery_detail_min_delay_seconds == 3.0
    assert settings.discovery_detail_cache_hours == 24


def test_jobintel_database_path_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBINTEL_DB_PATH", "/tmp/jobintel-test.db")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert load_jobintel_settings().jobintel_db_path == Path("/tmp/jobintel-test.db")
    assert load_jobintel_settings().llm_provider is JobIntelProviderName.OPENAI


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_max_iterations", 0),
        ("agent_max_repairs", -1),
        ("agent_max_tool_calls", 0),
        ("parser_max_repairs", -1),
        ("outreach_max_repairs", -1),
    ],
)
def test_jobintel_budgets_reject_invalid_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        JobIntelSettings(_env_file=None, **{field: value})


def test_discovery_pacing_rejects_unsafe_or_inverted_ranges() -> None:
    with pytest.raises(ValidationError):
        JobIntelSettings(_env_file=None, discovery_detail_min_delay_seconds=0)
    with pytest.raises(ValidationError, match="minimum delay"):
        JobIntelSettings(
            _env_file=None,
            discovery_search_min_delay_seconds=3,
            discovery_search_max_delay_seconds=2,
        )


def test_email_notification_readiness_and_auth_pair() -> None:
    settings = JobIntelSettings(
        _env_file=None,
        smtp_host="smtp.example.com",
        smtp_from_address="jobs@example.com",
    )
    assert settings.smtp_notification_ready is True
    with pytest.raises(ValidationError, match="username and password"):
        JobIntelSettings(_env_file=None, smtp_username="account")
