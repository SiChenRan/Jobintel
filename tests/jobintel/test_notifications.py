"""Offline tests for built-in discovery email notifications."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from jobintel.discovery.models import JobSearchPreference, JobSource, RawJobListing
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import EntityNotFoundError
from jobintel.notifications.address import mask_email_address
from jobintel.notifications.models import (
    CandidateEmailPreference,
    EmailNotificationStatus,
    SMTPTransport,
)
from jobintel.notifications.service import (
    DiscoveryEmailNotificationService,
    EmailNotificationError,
    render_discovery_email,
)
from jobintel.notifications.smtp import SMTPEmailSender
from jobintel.persistence.repository import SQLiteJobRepository

_NOW = datetime(2026, 9, 5, 16, 0, tzinfo=UTC)


@dataclass
class _Connector:
    source: JobSource = JobSource.BOSS

    def search(self, _preference: JobSearchPreference, *, limit: int) -> tuple[RawJobListing, ...]:
        return (
            RawJobListing(
                source=JobSource.BOSS,
                external_id="email-job-1",
                title="Python <平台> 工程师",
                company_name="星云 & 科技",
                location="上海",
                salary_text="25-40K",
                description="Python FastAPI",
                experience="3-5年",
                education="本科",
                url="https://www.zhipin.com/job_detail/email-job-1.html",
                published_text="今天",
            ),
        )[:limit]


@dataclass
class _Sender:
    from_address: str = "jobs@example.com"
    recipient: str = "owner@example.net"
    messages: list[EmailMessage] = field(default_factory=list)
    error: Exception | None = None

    def send(self, message: EmailMessage) -> None:
        if self.error:
            raise self.error
        self.messages.append(message)


def _run(repository: SQLiteJobRepository) -> str:
    run = JobDiscoveryService(repository, {JobSource.BOSS: _Connector()}).discover(
        JobSearchPreference(
            candidate_id="C001",
            profile_version=2,
            query="Python 后端",
            city="上海",
            sources=(JobSource.BOSS,),
            limit=10,
        )
    )
    return run.run_id


def test_email_service_sends_saved_jobs_and_persists_minimal_receipt(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    run_id = _run(jobintel_repo)
    sender = _Sender()
    times = iter((_NOW, _NOW + timedelta(seconds=1)))
    receipt = DiscoveryEmailNotificationService(
        jobintel_repo,
        sender,
        clock=lambda: next(times),
        id_factory=lambda: "email-test-success",
    ).send_discovery(run_id)

    assert receipt.status is EmailNotificationStatus.SENT
    assert receipt.recipient_masked == "o***@example.net"
    assert receipt.job_count == 1
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert "Python <平台> 工程师" in message.get_body(preferencelist=("plain",)).get_content()
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert "Python &lt;平台&gt; 工程师" in html_body
    assert "https://www.zhipin.com/job_detail/email-job-1.html" in html_body
    row = jobintel_repo._conn.execute(
        "SELECT * FROM email_notification_attempts WHERE notification_id = ?",
        (receipt.notification_id,),
    ).fetchone()
    assert "owner@example.net" not in " ".join(str(value) for value in row)


def test_candidate_email_preference_is_scoped_and_updatable(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    original = CandidateEmailPreference(
        candidate_id="C001",
        recipient_email="first@example.net",
        created_at=_NOW,
        updated_at=_NOW,
    )
    saved = jobintel_repo.save_candidate_email_preference(original)
    updated = jobintel_repo.save_candidate_email_preference(
        original.model_copy(
            update={
                "recipient_email": "second@example.org",
                "updated_at": _NOW + timedelta(minutes=1),
            }
        )
    )

    assert saved.recipient_email == "first@example.net"
    assert updated.recipient_email == "second@example.org"
    assert updated.created_at == _NOW
    assert jobintel_repo.find_candidate_email_preference("missing") is None


def test_candidate_email_preference_rejects_unknown_candidate(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    with pytest.raises(EntityNotFoundError, match="candidate profile not found"):
        jobintel_repo.save_candidate_email_preference(
            CandidateEmailPreference(
                candidate_id="missing",
                recipient_email="owner@example.net",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def test_email_service_records_safe_failure_and_raises(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    run_id = _run(jobintel_repo)
    sender = _Sender(error=OSError("private SMTP detail"))
    times = iter((_NOW, _NOW + timedelta(seconds=1)))
    service = DiscoveryEmailNotificationService(
        jobintel_repo,
        sender,
        clock=lambda: next(times),
        id_factory=lambda: "email-test-failure",
    )

    with pytest.raises(EmailNotificationError, match="SMTP"):
        service.send_discovery(run_id)

    receipt = jobintel_repo.get_email_notification_attempt("email-test-failure")
    assert receipt.status is EmailNotificationStatus.FAILED
    assert receipt.error_code == "OSError"
    assert "private SMTP detail" not in str(receipt)


def test_render_and_address_validation_reject_invalid_inputs(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    run = jobintel_repo.get_discovery_run(_run(jobintel_repo))
    with pytest.raises(ValueError, match="limit"):
        render_discovery_email(
            run,
            from_address="jobs@example.com",
            recipient="owner@example.net",
            limit=0,
        )
    with pytest.raises(ValueError, match="recipient"):
        mask_email_address("owner@example.net\nBcc: leak@example.com")
    with pytest.raises(ValueError, match="requires starttls or ssl"):
        SMTPEmailSender(
            host="smtp.example.com",
            port=25,
            transport=SMTPTransport.PLAIN,
            from_address="jobs@example.com",
            recipient="owner@example.net",
            username="account",
            password="secret",
        )


def test_smtp_sender_uses_starttls_and_fixed_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, *, context: object) -> None:
            calls.append(("starttls", context))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(
            self, message: EmailMessage, *, from_addr: str, to_addrs: list[str]
        ) -> None:
            calls.append(("send", message["Subject"], from_addr, to_addrs))

    monkeypatch.setattr(smtplib, "SMTP", lambda *_args, **_kwargs: _Client())
    sender = SMTPEmailSender(
        host="smtp.example.com",
        port=587,
        transport=SMTPTransport.STARTTLS,
        from_address="jobs@example.com",
        recipient="owner@example.net",
        username="account",
        password="secret",
    )
    message = EmailMessage()
    message["Subject"] = "职位通知"
    sender.send(message)

    assert calls[0] == "ehlo"
    assert calls[2] == "ehlo"
    assert ("login", "account", "secret") in calls
    assert calls[-1] == ("send", "职位通知", "jobs@example.com", ["owner@example.net"])
