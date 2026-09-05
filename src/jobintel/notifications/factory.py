"""Composition helper for the built-in SMTP notification adapter."""

from __future__ import annotations

from jobintel.config import JobIntelSettings
from jobintel.notifications.smtp import SMTPEmailSender


def build_email_sender(settings: JobIntelSettings, *, recipient: str) -> SMTPEmailSender:
    """Build a shared SMTP sender for one persisted candidate recipient."""
    if not settings.smtp_notification_ready:
        raise RuntimeError("邮件发送服务尚未配置, 请设置 SMTP_HOST 和 SMTP_FROM_ADDRESS")
    assert settings.smtp_host is not None
    assert settings.smtp_from_address is not None
    return SMTPEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        transport=settings.smtp_transport,
        from_address=settings.smtp_from_address,
        recipient=recipient,
        username=(settings.smtp_username or "").strip() or None,
        password=(
            (settings.smtp_password.get_secret_value().strip() or None)
            if settings.smtp_password is not None
            else None
        ),
        timeout_seconds=settings.smtp_timeout_seconds,
    )


__all__ = ["build_email_sender"]
