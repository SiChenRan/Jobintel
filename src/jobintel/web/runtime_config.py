"""Administrator-managed runtime configuration stored in the local database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import SecretStr

from jobintel.config import JobIntelSettings
from jobintel.persistence.db import JobIntelDatabase

RUNTIME_SETTING_FIELDS = frozenset(
    {
        "llm_provider",
        "anthropic_api_key",
        "anthropic_model",
        "openai_api_key",
        "openai_model",
        "deepseek_api_key",
        "deepseek_base_url",
        "deepseek_model",
        "smtp_host",
        "smtp_port",
        "smtp_transport",
        "smtp_username",
        "smtp_password",
        "smtp_from_address",
        "smtp_timeout_seconds",
        "discovery_cdp_port",
        "discovery_search_min_delay_seconds",
        "discovery_search_max_delay_seconds",
        "discovery_detail_min_delay_seconds",
        "discovery_detail_max_delay_seconds",
        "discovery_detail_cache_hours",
        "radar_min_interval_hours",
    }
)
SECRET_SETTING_FIELDS = frozenset(
    {"anthropic_api_key", "openai_api_key", "deepseek_api_key", "smtp_password"}
)


class RuntimeConfigStore:
    """Resolve validated database overrides on top of boot-time settings."""

    def __init__(self, database: JobIntelDatabase, base: JobIntelSettings) -> None:
        """Bind the migrated database and immutable boot-time fallback settings."""
        self._database = database
        self._base = base

    def resolve(self) -> JobIntelSettings:
        """Return the current validated settings without exposing storage details."""
        rows = self._database.connection.execute(
            "SELECT setting_key, setting_value_json FROM web_runtime_settings"
        ).fetchall()
        overrides = {
            str(row["setting_key"]): json.loads(str(row["setting_value_json"]))
            for row in rows
            if str(row["setting_key"]) in RUNTIME_SETTING_FIELDS
        }
        return _validated_settings(self._base, overrides)

    def update(self, values: dict[str, object], *, updated_by: str) -> JobIntelSettings:
        """Validate and atomically upsert an allow-listed set of runtime overrides."""
        unknown = set(values) - RUNTIME_SETTING_FIELDS
        if unknown:
            raise ValueError(f"不支持的环境配置: {', '.join(sorted(unknown))}")
        normalized = {
            key: _storage_value(value)
            for key, value in values.items()
            if not (key in SECRET_SETTING_FIELDS and value in (None, ""))
        }
        current_rows = self._database.connection.execute(
            "SELECT setting_key, setting_value_json FROM web_runtime_settings"
        ).fetchall()
        combined = {
            str(row["setting_key"]): json.loads(str(row["setting_value_json"]))
            for row in current_rows
        }
        combined.update(normalized)
        validated = _validated_settings(self._base, combined)
        now = datetime.now(UTC).isoformat()
        with self._database.transaction():
            for key, value in normalized.items():
                self._database.connection.execute(
                    """
                    INSERT INTO web_runtime_settings (
                        setting_key, setting_value_json, is_secret, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value_json = excluded.setting_value_json,
                        is_secret = excluded.is_secret,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (
                        key,
                        json.dumps(value, ensure_ascii=False),
                        int(key in SECRET_SETTING_FIELDS),
                        now,
                        updated_by,
                    ),
                )
        return validated


def safe_runtime_payload(settings: JobIntelSettings) -> dict[str, object]:
    """Return editable settings while representing secrets only as configured flags."""
    return {
        "llm_provider": settings.llm_provider.value,
        "anthropic_api_key_configured": bool(settings.anthropic_api_key),
        "anthropic_model": settings.anthropic_model,
        "openai_api_key_configured": bool(settings.openai_api_key),
        "openai_model": settings.openai_model,
        "deepseek_api_key_configured": bool(settings.deepseek_api_key),
        "deepseek_base_url": settings.deepseek_base_url,
        "deepseek_model": settings.deepseek_model,
        "smtp_host": settings.smtp_host or "",
        "smtp_port": settings.smtp_port,
        "smtp_transport": settings.smtp_transport.value,
        "smtp_username": settings.smtp_username or "",
        "smtp_password_configured": settings.smtp_password is not None,
        "smtp_from_address": settings.smtp_from_address or "",
        "smtp_timeout_seconds": settings.smtp_timeout_seconds,
        "smtp_notification_ready": settings.smtp_notification_ready,
        "discovery_cdp_port": settings.discovery_cdp_port,
        "discovery_search_min_delay_seconds": settings.discovery_search_min_delay_seconds,
        "discovery_search_max_delay_seconds": settings.discovery_search_max_delay_seconds,
        "discovery_detail_min_delay_seconds": settings.discovery_detail_min_delay_seconds,
        "discovery_detail_max_delay_seconds": settings.discovery_detail_max_delay_seconds,
        "discovery_detail_cache_hours": settings.discovery_detail_cache_hours,
        "radar_min_interval_hours": settings.radar_min_interval_hours,
    }


def _validated_settings(base: JobIntelSettings, overrides: dict[str, object]) -> JobIntelSettings:
    payload = base.model_dump()
    payload.update(
        {key: value for key, value in overrides.items() if key in RUNTIME_SETTING_FIELDS}
    )
    return JobIntelSettings.model_validate(payload)


def _storage_value(value: object) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        return value.value
    return value


__all__ = [
    "RUNTIME_SETTING_FIELDS",
    "SECRET_SETTING_FIELDS",
    "RuntimeConfigStore",
    "safe_runtime_payload",
]
