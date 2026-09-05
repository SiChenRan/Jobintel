"""Deterministic channel rendering and local product limits."""

from __future__ import annotations

from dataclasses import dataclass

from jobintel.outreach.models import OutreachChannel, OutreachMessageDraft


class OutreachChannelPolicyError(ValueError):
    """Raised when a draft cannot be safely rendered for its channel."""


@dataclass(frozen=True)
class OutreachChannelPolicy:
    """Local product constraints, independent from undocumented platform limits."""

    channel: OutreachChannel
    max_message_chars: int
    max_claims: int


BOSS_DRAFT_POLICY = OutreachChannelPolicy(
    channel=OutreachChannel.BOSS,
    max_message_chars=500,
    max_claims=3,
)


def _line(value: str) -> str:
    """Collapse whitespace while preserving explicit sentence punctuation."""
    return " ".join(value.split())


def render_outreach_message(
    draft: OutreachMessageDraft,
    *,
    policy: OutreachChannelPolicy = BOSS_DRAFT_POLICY,
) -> str:
    """Render structured content without allowing the model to own final composition."""
    if len(draft.claims) > policy.max_claims:
        raise OutreachChannelPolicyError(
            f"{policy.channel.value} outreach allows at most {policy.max_claims} claims"
        )
    message = "\n".join(
        (
            _line(draft.salutation),
            _line(draft.motivation),
            *(_line(claim.text) for claim in draft.claims),
            _line(draft.conversation_opener),
            _line(draft.closing),
        )
    )
    if len(message) > policy.max_message_chars:
        raise OutreachChannelPolicyError(
            f"{policy.channel.value} outreach exceeds local "
            f"{policy.max_message_chars}-character policy"
        )
    return message
