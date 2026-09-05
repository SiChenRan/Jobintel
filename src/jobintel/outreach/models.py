"""Strict domain contracts for evidence-grounded HR outreach drafts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from jobintel.models import FrozenDomainModel, NonEmptyStr, Sha256Hex, UtcDateTime

OUTREACH_ID_VERSION = "jobintel-outreach-id-v1"
OUTREACH_EVENT_ID_VERSION = "jobintel-outreach-event-id-v1"
OUTREACH_SCHEMA_VERSION = "jobintel-outreach-v1"


class OutreachChannel(StrEnum):
    """Supported presentation channels for an outreach draft."""

    BOSS = "boss"


class OutreachTone(StrEnum):
    """Requested writing style without changing factual content."""

    CONCISE = "concise"
    PROFESSIONAL = "professional"
    TECHNICAL = "technical"


class OutreachStatus(StrEnum):
    """Review state of one immutable outreach revision."""

    DRAFT = "draft"
    APPROVED = "approved"
    SENT_CONFIRMED = "sent_confirmed"
    DISMISSED = "dismissed"


class OutreachEventType(StrEnum):
    """Auditable user actions associated with an outreach revision."""

    APPROVED = "approved"
    COPIED = "copied"
    OPENED = "opened"
    SENT_CONFIRMED = "sent_confirmed"
    DISMISSED = "dismissed"


class OutreachClaimDraft(FrozenDomainModel):
    """One model-authored factual sentence and its proposed citations."""

    text: NonEmptyStr
    requirement_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("requirement_ids", "evidence_ids")
    @classmethod
    def unique_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate references without changing their display order."""
        if len(values) != len(set(values)):
            raise ValueError("outreach claim references must be unique")
        return values


class OutreachMessageDraft(FrozenDomainModel):
    """Model-authored content before deterministic validation and rendering.

    Candidate facts may appear only in ``claims``. The remaining fields provide
    salutation, role motivation, a conversation question, and a closing.
    """

    salutation: NonEmptyStr
    motivation: NonEmptyStr
    claims: tuple[OutreachClaimDraft, ...] = Field(min_length=1, max_length=5)
    conversation_opener: NonEmptyStr
    closing: NonEmptyStr

    @field_validator("claims")
    @classmethod
    def unique_claim_text(
        cls, claims: tuple[OutreachClaimDraft, ...]
    ) -> tuple[OutreachClaimDraft, ...]:
        """Reject repeated claim sentences in a single message."""
        normalized = tuple(" ".join(claim.text.split()).casefold() for claim in claims)
        if len(normalized) != len(set(normalized)):
            raise ValueError("outreach claim texts must be unique")
        return claims


class OutreachClaim(OutreachClaimDraft):
    """Program-identified claim retained in a finalized outreach revision."""

    claim_id: NonEmptyStr
    source_order: int = Field(ge=0)


class OutreachDraft(FrozenDomainModel):
    """Program-authored, persistence-ready outreach revision."""

    outreach_id: NonEmptyStr
    revision: int = Field(ge=1)
    analysis_id: NonEmptyStr
    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    channel: OutreachChannel
    tone: OutreachTone
    salutation: NonEmptyStr
    motivation: NonEmptyStr
    claims: tuple[OutreachClaim, ...] = Field(min_length=1, max_length=5)
    conversation_opener: NonEmptyStr
    closing: NonEmptyStr
    rendered_message: NonEmptyStr
    user_edited_message: NonEmptyStr | None = None
    status: OutreachStatus
    provider: NonEmptyStr
    prompt_version: NonEmptyStr
    schema_version: NonEmptyStr = OUTREACH_SCHEMA_VERSION
    provenance_digest: Sha256Hex
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_revision_content(self) -> OutreachDraft:
        """Keep claim order and edit/status metadata coherent."""
        orders = tuple(claim.source_order for claim in self.claims)
        if orders != tuple(range(len(self.claims))):
            raise ValueError("outreach claim source_order must be contiguous from zero")
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("outreach claim_id values must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("outreach updated_at cannot precede created_at")
        return self

    @property
    def effective_message(self) -> str:
        """Return the user-edited message when present, otherwise the guarded rendering."""
        return self.user_edited_message or self.rendered_message

    @property
    def is_user_edited(self) -> bool:
        """Return whether this revision contains text outside the generated guarantee."""
        return self.user_edited_message is not None


class OutreachEventAttribute(FrozenDomainModel):
    """One non-sensitive, query-independent event annotation."""

    key: NonEmptyStr
    value: NonEmptyStr


class OutreachEvent(FrozenDomainModel):
    """One append-only user action against an exact outreach revision."""

    event_id: NonEmptyStr
    outreach_id: NonEmptyStr
    revision: int = Field(ge=1)
    event_type: OutreachEventType
    from_status: OutreachStatus
    to_status: OutreachStatus
    attributes: tuple[OutreachEventAttribute, ...] = ()
    created_at: UtcDateTime

    @field_validator("attributes")
    @classmethod
    def unique_attribute_keys(
        cls, values: tuple[OutreachEventAttribute, ...]
    ) -> tuple[OutreachEventAttribute, ...]:
        """Reject ambiguous duplicate metadata keys."""
        keys = tuple(item.key for item in values)
        if len(keys) != len(set(keys)):
            raise ValueError("outreach event attribute keys must be unique")
        return values


def stable_outreach_id(run_id: str) -> str:
    """Derive a program-controlled outreach identity from a generation run key."""
    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty")
    payload = f"{OUTREACH_ID_VERSION}\n{normalized}"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"outreach_{digest[:20]}"


def stable_outreach_claim_id(*, outreach_id: str, revision: int, source_order: int) -> str:
    """Derive a claim ID from program-controlled revision coordinates."""
    normalized = outreach_id.strip()
    if not normalized:
        raise ValueError("outreach_id must not be empty")
    if revision < 1:
        raise ValueError("revision must be at least 1")
    if source_order < 0:
        raise ValueError("source_order must not be negative")
    payload = json.dumps(
        {
            "outreach_id": normalized,
            "revision": revision,
            "source_order": source_order,
            "version": OUTREACH_ID_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"oclaim_{digest[:20]}"


def stable_outreach_event_id(event_key: str) -> str:
    """Derive an event identity from a caller-controlled idempotency key."""
    normalized = event_key.strip()
    if not normalized:
        raise ValueError("event_key must not be empty")
    payload = f"{OUTREACH_EVENT_ID_VERSION}\n{normalized}"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"oevent_{digest[:20]}"
