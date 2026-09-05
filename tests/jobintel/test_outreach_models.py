"""Tests for outreach identities, immutable models, rendering, and state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from jobintel.outreach.models import (
    OUTREACH_SCHEMA_VERSION,
    OutreachChannel,
    OutreachClaim,
    OutreachClaimDraft,
    OutreachDraft,
    OutreachEventType,
    OutreachMessageDraft,
    OutreachStatus,
    OutreachTone,
    stable_outreach_claim_id,
    stable_outreach_id,
)
from jobintel.outreach.policy import (
    BOSS_DRAFT_POLICY,
    OutreachChannelPolicy,
    OutreachChannelPolicyError,
    render_outreach_message,
)
from jobintel.outreach.state import (
    OutreachStateTransitionError,
    transition_outreach_status,
    validate_outreach_event,
)


def _message(*, claim_count: int = 1) -> OutreachMessageDraft:
    return OutreachMessageDraft(
        salutation="您好",
        motivation="我对这个后端岗位很感兴趣。",
        claims=tuple(
            OutreachClaimDraft(
                text=f"我具备第{index + 1}项相关项目经验。",
                requirement_ids=(f"req-{index}",),
                evidence_ids=(f"ev-{index}",),
            )
            for index in range(claim_count)
        ),
        conversation_opener="想了解团队当前最关注的技术挑战是什么?",
        closing="期待与您进一步交流。",
    )


def test_outreach_and_claim_ids_are_stable_and_program_controlled() -> None:
    outreach_id = stable_outreach_id(" generation-run-1 ")
    assert outreach_id == stable_outreach_id("generation-run-1")
    assert outreach_id.startswith("outreach_")

    claim_id = stable_outreach_claim_id(outreach_id=outreach_id, revision=1, source_order=0)
    assert claim_id == stable_outreach_claim_id(outreach_id=outreach_id, revision=1, source_order=0)
    assert claim_id != stable_outreach_claim_id(outreach_id=outreach_id, revision=2, source_order=0)

    with pytest.raises(ValueError, match="run_id"):
        stable_outreach_id(" ")
    with pytest.raises(ValueError, match="revision"):
        stable_outreach_claim_id(outreach_id=outreach_id, revision=0, source_order=0)
    with pytest.raises(ValueError, match="outreach_id"):
        stable_outreach_claim_id(outreach_id=" ", revision=1, source_order=0)
    with pytest.raises(ValueError, match="source_order"):
        stable_outreach_claim_id(outreach_id=outreach_id, revision=1, source_order=-1)


def test_message_rejects_duplicate_references_and_claims() -> None:
    with pytest.raises(ValidationError, match="references must be unique"):
        OutreachClaimDraft(
            text="我有相关经验。",
            requirement_ids=("req-1", "req-1"),
            evidence_ids=("ev-1",),
        )

    claim = OutreachClaimDraft(
        text="我有相关经验。",
        requirement_ids=("req-1",),
        evidence_ids=("ev-1",),
    )
    with pytest.raises(ValidationError, match="claim texts must be unique"):
        OutreachMessageDraft(
            salutation="您好",
            motivation="我对岗位很感兴趣。",
            claims=(claim, claim),
            conversation_opener="可以进一步沟通吗?",
            closing="谢谢。",
        )


def test_channel_policy_renders_structure_and_enforces_local_limits() -> None:
    message = _message()
    rendered = render_outreach_message(message)
    assert rendered.splitlines() == [
        message.salutation,
        message.motivation,
        message.claims[0].text,
        message.conversation_opener,
        message.closing,
    ]
    assert len(rendered) <= BOSS_DRAFT_POLICY.max_message_chars

    with pytest.raises(OutreachChannelPolicyError, match="at most 3 claims"):
        render_outreach_message(_message(claim_count=4))
    with pytest.raises(OutreachChannelPolicyError, match="exceeds local"):
        render_outreach_message(
            message,
            policy=OutreachChannelPolicy(
                channel=OutreachChannel.BOSS,
                max_message_chars=10,
                max_claims=3,
            ),
        )


def test_outreach_status_and_events_require_explicit_approval() -> None:
    assert (
        transition_outreach_status(OutreachStatus.DRAFT, OutreachStatus.APPROVED)
        is OutreachStatus.APPROVED
    )
    assert (
        transition_outreach_status(OutreachStatus.APPROVED, OutreachStatus.SENT_CONFIRMED)
        is OutreachStatus.SENT_CONFIRMED
    )
    validate_outreach_event(OutreachStatus.APPROVED, OutreachEventType.COPIED)
    validate_outreach_event(OutreachStatus.APPROVED, OutreachEventType.OPENED)
    validate_outreach_event(OutreachStatus.DRAFT, OutreachEventType.APPROVED)
    validate_outreach_event(OutreachStatus.DRAFT, OutreachEventType.DISMISSED)
    validate_outreach_event(OutreachStatus.APPROVED, OutreachEventType.SENT_CONFIRMED)
    assert (
        transition_outreach_status(OutreachStatus.APPROVED, OutreachStatus.DISMISSED)
        is OutreachStatus.DISMISSED
    )

    with pytest.raises(OutreachStateTransitionError, match="draft -> sent_confirmed"):
        transition_outreach_status(OutreachStatus.DRAFT, OutreachStatus.SENT_CONFIRMED)
    with pytest.raises(OutreachStateTransitionError, match="requires an approved"):
        validate_outreach_event(OutreachStatus.DRAFT, OutreachEventType.COPIED)
    with pytest.raises(OutreachStateTransitionError, match="sent_confirmed -> dismissed"):
        transition_outreach_status(OutreachStatus.SENT_CONFIRMED, OutreachStatus.DISMISSED)
    with pytest.raises(OutreachStateTransitionError, match="unknown outreach event"):
        validate_outreach_event(OutreachStatus.APPROVED, object())  # type: ignore[arg-type]


def test_final_outreach_revision_is_immutable_and_tracks_user_edits() -> None:
    message = _message()
    outreach_id = stable_outreach_id("run-final")
    now = datetime(2026, 9, 5, tzinfo=UTC)
    claim = OutreachClaim(
        **message.claims[0].model_dump(),
        claim_id=stable_outreach_claim_id(outreach_id=outreach_id, revision=1, source_order=0),
        source_order=0,
    )
    outreach = OutreachDraft(
        outreach_id=outreach_id,
        revision=1,
        analysis_id="analysis-1",
        job_id="job-1",
        job_version=1,
        candidate_id="candidate-1",
        profile_version=1,
        channel=OutreachChannel.BOSS,
        tone=OutreachTone.PROFESSIONAL,
        salutation=message.salutation,
        motivation=message.motivation,
        claims=(claim,),
        conversation_opener=message.conversation_opener,
        closing=message.closing,
        rendered_message=render_outreach_message(message),
        status=OutreachStatus.DRAFT,
        provider="deepseek",
        prompt_version="prompt-v1",
        provenance_digest="a" * 64,
        created_at=now,
        updated_at=now,
    )
    assert outreach.schema_version == OUTREACH_SCHEMA_VERSION
    assert outreach.effective_message == outreach.rendered_message
    assert outreach.is_user_edited is False

    edited = outreach.model_copy(update={"user_edited_message": "这是用户修改后的文案。"})
    assert edited.effective_message == "这是用户修改后的文案。"
    assert edited.is_user_edited is True
    with pytest.raises(ValidationError, match="frozen"):
        outreach.status = OutreachStatus.APPROVED  # type: ignore[misc]

    with pytest.raises(ValidationError, match="contiguous"):
        OutreachDraft(
            **{**outreach.model_dump(), "claims": (claim.model_copy(update={"source_order": 1}),)}
        )
    duplicate_claim = claim.model_copy(update={"text": "另一项有证据的能力。", "source_order": 1})
    with pytest.raises(ValidationError, match="claim_id values must be unique"):
        OutreachDraft(**{**outreach.model_dump(), "claims": (claim, duplicate_claim)})
    with pytest.raises(ValidationError, match="cannot precede"):
        OutreachDraft(**{**outreach.model_dump(), "updated_at": now - timedelta(seconds=1)})
