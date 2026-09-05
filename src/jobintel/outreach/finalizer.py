"""Program-owned finalization of validated model-authored outreach content."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from jobintel.models import CandidateProfile, JobAnalysis, JobPosting
from jobintel.outreach.guardrail import OUTREACH_GUARDRAIL_VERSION
from jobintel.outreach.models import (
    OUTREACH_SCHEMA_VERSION,
    OutreachChannel,
    OutreachClaim,
    OutreachDraft,
    OutreachMessageDraft,
    OutreachStatus,
    OutreachTone,
    stable_outreach_claim_id,
    stable_outreach_id,
)
from jobintel.outreach.policy import BOSS_DRAFT_POLICY, render_outreach_message


def finalize_outreach(
    *,
    submission: OutreachMessageDraft,
    analysis: JobAnalysis,
    job: JobPosting,
    profile: CandidateProfile,
    tone: OutreachTone,
    provider: str,
    prompt_version: str,
    run_id: str,
    created_at: datetime,
) -> OutreachDraft:
    """Assign identities, render text, and bind a draft to immutable source versions."""
    outreach_id = stable_outreach_id(run_id)
    revision = 1
    claims = tuple(
        OutreachClaim(
            claim_id=stable_outreach_claim_id(
                outreach_id=outreach_id,
                revision=revision,
                source_order=source_order,
            ),
            source_order=source_order,
            **claim.model_dump(),
        )
        for source_order, claim in enumerate(submission.claims)
    )
    rendered_message = render_outreach_message(submission, policy=BOSS_DRAFT_POLICY)
    provenance_payload = json.dumps(
        {
            "analysis_id": analysis.analysis_id,
            "analysis_provenance_digest": analysis.provenance_digest,
            "job_id": job.job_id,
            "job_version": job.job_version,
            "candidate_id": profile.candidate_id,
            "profile_version": profile.profile_version,
            "submission": submission.model_dump(mode="json"),
            "provider": provider,
            "prompt_version": prompt_version,
            "guardrail_version": OUTREACH_GUARDRAIL_VERSION,
            "schema_version": OUTREACH_SCHEMA_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    provenance_digest = hashlib.sha256(provenance_payload.encode("utf-8")).hexdigest()
    return OutreachDraft(
        outreach_id=outreach_id,
        revision=revision,
        analysis_id=analysis.analysis_id,
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        channel=OutreachChannel.BOSS,
        tone=tone,
        salutation=submission.salutation,
        motivation=submission.motivation,
        claims=claims,
        conversation_opener=submission.conversation_opener,
        closing=submission.closing,
        rendered_message=rendered_message,
        status=OutreachStatus.DRAFT,
        provider=provider,
        prompt_version=prompt_version,
        provenance_digest=provenance_digest,
        created_at=created_at,
        updated_at=created_at,
    )
