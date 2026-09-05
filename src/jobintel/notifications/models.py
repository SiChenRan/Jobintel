"""Domain contracts for auditable email notification attempts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from jobintel.models import FrozenDomainModel, NonEmptyStr, Sha256Hex, UtcDateTime
from jobintel.notifications.address import validate_email_address


class SMTPTransport(StrEnum):
    """Supported secure SMTP connection modes."""

    STARTTLS = "starttls"
    SSL = "ssl"
    PLAIN = "plain"


class EmailNotificationStatus(StrEnum):
    """Delivery state recorded by the local SMTP client."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class CandidateEmailPreference(FrozenDomainModel):
    """One candidate's persisted recipient mailbox for job notifications."""

    candidate_id: NonEmptyStr
    recipient_email: NonEmptyStr = Field(max_length=320)
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @field_validator("recipient_email")
    @classmethod
    def valid_recipient_email(cls, value: str) -> str:
        """Normalize and validate the stored mailbox address."""
        return validate_email_address(value, label="recipient")

    @model_validator(mode="after")
    def coherent_timestamps(self) -> CandidateEmailPreference:
        """Prevent an update timestamp from preceding creation."""
        if self.updated_at < self.created_at:
            raise ValueError("email preference updated_at cannot precede created_at")
        return self


class EmailNotificationAttempt(FrozenDomainModel):
    """One delivery attempt without storing message or recipient content."""

    notification_id: NonEmptyStr
    discovery_run_id: NonEmptyStr
    recipient_masked: NonEmptyStr
    job_count: int = Field(ge=1, le=500)
    status: EmailNotificationStatus
    subject_sha256: Sha256Hex
    error_code: NonEmptyStr | None = None
    created_at: UtcDateTime
    completed_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def coherent_completion(self) -> EmailNotificationAttempt:
        """Require completion metadata only for terminal attempts."""
        terminal = self.status in (
            EmailNotificationStatus.SENT,
            EmailNotificationStatus.FAILED,
        )
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal email notification requires completed_at")
        if (self.status is EmailNotificationStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only failed email notifications may contain error_code")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("email notification completed_at cannot precede created_at")
        return self
