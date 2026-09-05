"""Render and deliver saved discovery results as concise email notifications."""

from __future__ import annotations

import hashlib
import html
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Protocol
from uuid import uuid4

from jobintel.discovery.models import DiscoveryRun
from jobintel.notifications.address import mask_email_address
from jobintel.notifications.models import EmailNotificationAttempt, EmailNotificationStatus
from jobintel.notifications.smtp import SMTPEmailSender
from jobintel.persistence.repository import SQLiteJobRepository


class EmailSender(Protocol):
    """Minimal delivery boundary used by the notification service."""

    from_address: str
    recipient: str

    def send(self, message: EmailMessage) -> None:
        """Deliver one fully prepared message."""
        ...


class EmailNotificationError(RuntimeError):
    """Raised after a failed SMTP attempt has been recorded safely."""


def render_discovery_email(
    run: DiscoveryRun,
    *,
    from_address: str,
    recipient: str,
    limit: int,
) -> EmailMessage:
    """Build plain-text and HTML alternatives from one saved discovery run."""
    if not 1 <= limit <= 500:
        raise ValueError("email job limit must be between 1 and 500")
    hits = run.hits[:limit]
    if not hits:
        raise ValueError("discovery run has no jobs to notify")
    query = " ".join(run.preference.query.split())
    location = " ".join((run.preference.city or "全部地区").split())
    subject = f"[JobIntel] {query} · {location} · {len(hits)} 个职位"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = recipient

    plain_rows = [f"{query} · {location}", ""]
    html_rows = []
    for index, hit in enumerate(hits, start=1):
        job = hit.job
        url = str(job.source_links[0].url)
        details = " · ".join(
            value
            for value in (job.location, job.salary_text, job.experience, job.education)
            if value
        )
        plain_rows.extend(
            (
                f"{index}. {job.title} | {job.company_name}",
                details,
                url,
                "",
            )
        )
        html_rows.append(
            "<li>"
            f"<p><strong>{html.escape(job.title)}</strong> · "
            f"{html.escape(job.company_name)}</p>"
            f"<p>{html.escape(details)}</p>"
            f'<p><a href="{html.escape(url, quote=True)}">查看职位</a></p>'
            "</li>"
        )
    message.set_content("\n".join(plain_rows).strip() + "\n")
    message.add_alternative(
        "<!doctype html><html><body>"
        f"<h2>{html.escape(query)} · {html.escape(location)}</h2>"
        f"<ol>{''.join(html_rows)}</ol>"
        "</body></html>",
        subtype="html",
    )
    return message


class DiscoveryEmailNotificationService:
    """Send a saved discovery batch and persist a content-minimal audit receipt."""

    def __init__(
        self,
        repository: SQLiteJobRepository,
        sender: EmailSender,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Configure repository, SMTP boundary, and deterministic test controls."""
        self._repository = repository
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: f"email_{uuid4().hex}")

    def send_discovery(self, run_id: str, *, limit: int = 50) -> EmailNotificationAttempt:
        """Send one saved result batch to the configured notification mailbox."""
        run = self._repository.get_discovery_run(run_id)
        message = render_discovery_email(
            run,
            from_address=self._sender.from_address,
            recipient=self._sender.recipient,
            limit=limit,
        )
        now = self._clock()
        attempt = EmailNotificationAttempt(
            notification_id=self._id_factory(),
            discovery_run_id=run_id,
            recipient_masked=mask_email_address(self._sender.recipient),
            job_count=min(len(run.hits), limit),
            status=EmailNotificationStatus.PENDING,
            subject_sha256=hashlib.sha256(str(message["Subject"]).encode("utf-8")).hexdigest(),
            created_at=now,
        )
        self._repository.save_email_notification_attempt(attempt)
        try:
            self._sender.send(message)
        except Exception as exc:
            failed = EmailNotificationAttempt.model_validate(
                {
                    **attempt.model_dump(),
                    "status": EmailNotificationStatus.FAILED,
                    "error_code": type(exc).__name__,
                    "completed_at": self._clock(),
                }
            )
            self._repository.complete_email_notification_attempt(failed)
            raise EmailNotificationError("邮件发送失败, 请检查 SMTP 配置和网络连接") from exc
        sent = EmailNotificationAttempt.model_validate(
            {
                **attempt.model_dump(),
                "status": EmailNotificationStatus.SENT,
                "completed_at": self._clock(),
            }
        )
        return self._repository.complete_email_notification_attempt(sent)


__all__ = [
    "DiscoveryEmailNotificationService",
    "EmailNotificationError",
    "EmailSender",
    "SMTPEmailSender",
    "render_discovery_email",
]
