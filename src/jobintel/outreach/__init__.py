"""Evidence-grounded HR outreach draft primitives."""

from jobintel.outreach.models import (
    OUTREACH_SCHEMA_VERSION,
    OutreachChannel,
    OutreachClaim,
    OutreachClaimDraft,
    OutreachDraft,
    OutreachEvent,
    OutreachEventAttribute,
    OutreachEventType,
    OutreachMessageDraft,
    OutreachStatus,
    OutreachTone,
    stable_outreach_claim_id,
    stable_outreach_event_id,
    stable_outreach_id,
)

__all__ = [
    "OUTREACH_SCHEMA_VERSION",
    "OutreachChannel",
    "OutreachClaim",
    "OutreachClaimDraft",
    "OutreachDraft",
    "OutreachEvent",
    "OutreachEventAttribute",
    "OutreachEventType",
    "OutreachMessageDraft",
    "OutreachStatus",
    "OutreachTone",
    "stable_outreach_claim_id",
    "stable_outreach_event_id",
    "stable_outreach_id",
]
