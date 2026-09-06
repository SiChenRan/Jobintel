"""Configuration for the independent JobIntel persistence stack."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobintel.notifications.models import SMTPTransport


class JobIntelProviderName(StrEnum):
    """Supported provider adapters for JobIntel live runs."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class JobIntelSettings(BaseSettings):
    """Strongly typed JobIntel settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    jobintel_db_path: Path = Field(default=Path("data/jobintel.db"))
    llm_provider: JobIntelProviderName = JobIntelProviderName.ANTHROPIC
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    agent_max_iterations: int = Field(default=12, ge=1)
    agent_max_repairs: int = Field(default=2, ge=0)
    agent_max_tool_calls: int = Field(default=60, ge=1)
    parser_max_repairs: int = Field(default=2, ge=0)
    outreach_max_repairs: int = Field(default=2, ge=0)
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_transport: SMTPTransport = SMTPTransport.STARTTLS
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_address: str | None = None
    smtp_timeout_seconds: float = Field(default=15, gt=0, le=120)
    discovery_cdp_port: int = Field(default=9222, ge=1, le=65535)
    discovery_timeout_seconds: float = Field(default=12.0, gt=0, le=120)
    discovery_max_workers: int = Field(default=4, ge=1, le=8)
    discovery_search_min_delay_seconds: float = Field(default=1.2, ge=1.0, le=30)
    discovery_search_max_delay_seconds: float = Field(default=2.4, ge=1.0, le=30)
    discovery_detail_min_delay_seconds: float = Field(default=3.0, ge=2.0, le=60)
    discovery_detail_max_delay_seconds: float = Field(default=6.0, ge=2.0, le=60)
    discovery_detail_cache_hours: int = Field(default=24, ge=1, le=168)
    radar_min_interval_hours: int = Field(default=6, ge=1, le=168)
    web_session_hours: int = Field(default=168, ge=1, le=720)
    web_cookie_secure: bool = False

    @model_validator(mode="after")
    def valid_discovery_delay_ranges(self) -> JobIntelSettings:
        """Reject inverted pacing ranges before a source connector is built."""
        if self.discovery_search_min_delay_seconds > self.discovery_search_max_delay_seconds:
            raise ValueError("discovery search minimum delay cannot exceed maximum")
        if self.discovery_detail_min_delay_seconds > self.discovery_detail_max_delay_seconds:
            raise ValueError("discovery detail minimum delay cannot exceed maximum")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("SMTP username and password must be configured together")
        return self

    @property
    def smtp_notification_ready(self) -> bool:
        """Return whether the shared SMTP sender settings are complete."""
        return all(
            value and value.strip()
            for value in (
                self.smtp_host,
                self.smtp_from_address,
            )
        )

    def require_anthropic_key(self) -> str:
        """Return the Anthropic key or raise a safe configuration error."""
        if not self.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to the local .env file.")
        return self.anthropic_api_key

    def require_openai_key(self) -> str:
        """Return the OpenAI key or raise a safe configuration error."""
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to the local .env file.")
        return self.openai_api_key

    def require_deepseek_key(self) -> str:
        """Return the DeepSeek key or raise a safe configuration error."""
        if not self.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Add it to the local .env file.")
        return self.deepseek_api_key


def load_jobintel_settings() -> JobIntelSettings:
    """Load JobIntel settings from the process environment and local .env."""
    return JobIntelSettings()
