"""Adversarial tests for evidence-grounded outreach messages."""

from __future__ import annotations

from tests.jobintel.outreach_fixtures import build_outreach_scope, valid_outreach_message

from jobintel.models import MatchStatus, RequirementMatchDraft
from jobintel.outreach.guardrail import OutreachViolationCode, validate_outreach_message
from jobintel.outreach.models import OutreachChannel, OutreachClaimDraft
from jobintel.outreach.policy import OutreachChannelPolicy
from jobintel.persistence.repository import SQLiteJobRepository


def _codes(result: object) -> set[OutreachViolationCode]:
    return {item.code for item in result.violations}  # type: ignore[attr-defined]


def test_valid_grounded_message_passes(jobintel_repo: SQLiteJobRepository) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    result = validate_outreach_message(
        draft=valid_outreach_message(job),
        analysis=analysis,
        job=job,
        profile=profile,
    )
    assert result.is_valid is True
    assert result.violations == ()


def test_scope_mismatches_are_rejected(jobintel_repo: SQLiteJobRepository) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    wrong_analysis = analysis.model_copy(
        update={"job_id": "wrong-job", "candidate_id": "wrong-candidate"}
    )
    result = validate_outreach_message(
        draft=valid_outreach_message(job),
        analysis=wrong_analysis,
        job=job,
        profile=profile,
    )
    assert _codes(result) >= {
        OutreachViolationCode.ANALYSIS_JOB_SCOPE_MISMATCH,
        OutreachViolationCode.ANALYSIS_PROFILE_SCOPE_MISMATCH,
    }


def test_unknown_and_wrong_scope_references_are_rejected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    message = valid_outreach_message(job)
    unknown = message.model_copy(
        update={
            "claims": (
                OutreachClaimDraft(
                    text="我有相关后端经验。",
                    requirement_ids=("req-invented",),
                    evidence_ids=("ev-invented",),
                ),
            )
        }
    )
    unknown_result = validate_outreach_message(
        draft=unknown, analysis=analysis, job=job, profile=profile
    )
    assert _codes(unknown_result) >= {
        OutreachViolationCode.UNKNOWN_REQUIREMENT,
        OutreachViolationCode.UNKNOWN_EVIDENCE,
    }

    wrong_evidence = message.model_copy(
        update={
            "claims": (
                message.claims[0].model_copy(update={"evidence_ids": ("ev-atlas-platform",)}),
            )
        }
    )
    wrong_result = validate_outreach_message(
        draft=wrong_evidence, analysis=analysis, job=job, profile=profile
    )
    assert _codes(wrong_result) >= {
        OutreachViolationCode.REQUIREMENT_WITHOUT_CLAIM_EVIDENCE,
        OutreachViolationCode.EVIDENCE_NOT_MATCHED_TO_REQUIREMENT,
    }


def test_each_referenced_requirement_needs_its_own_support(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    message = valid_outreach_message(job)
    claim = message.claims[0].model_copy(
        update={
            "requirement_ids": (
                job.requirements[0].requirement_id,
                job.requirements[1].requirement_id,
            )
        }
    )
    result = validate_outreach_message(
        draft=message.model_copy(update={"claims": (claim,)}),
        analysis=analysis,
        job=job,
        profile=profile,
    )
    violations = [
        item
        for item in result.violations
        if item.code is OutreachViolationCode.REQUIREMENT_WITHOUT_CLAIM_EVIDENCE
    ]
    assert [item.requirement_id for item in violations] == [job.requirements[1].requirement_id]


def test_missing_requirement_and_partial_overstatement_are_rejected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    missing_claim = OutreachClaimDraft(
        text="我具备 Kubernetes 部署经验。",
        requirement_ids=(job.requirements[3].requirement_id,),
        evidence_ids=("ev-atlas-platform",),
    )
    missing_result = validate_outreach_message(
        draft=valid_outreach_message(job).model_copy(update={"claims": (missing_claim,)}),
        analysis=analysis,
        job=job,
        profile=profile,
    )
    assert OutreachViolationCode.REQUIREMENT_NOT_POSITIVE in _codes(missing_result)

    partial_claim = OutreachClaimDraft(
        text="我是 PostgreSQL 专家, 完全胜任相关工作。",
        requirement_ids=(job.requirements[2].requirement_id,),
        evidence_ids=("ev-python-api",),
    )
    partial_result = validate_outreach_message(
        draft=valid_outreach_message(job).model_copy(update={"claims": (partial_claim,)}),
        analysis=analysis,
        job=job,
        profile=profile,
    )
    assert OutreachViolationCode.PARTIAL_MATCH_OVERSTATED in _codes(partial_result)


def test_recruiter_name_language_and_channel_policy_are_guarded(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    message = valid_outreach_message(job)
    invented = message.model_copy(update={"salutation": "李经理您好"})
    invented_result = validate_outreach_message(
        draft=invented, analysis=analysis, job=job, profile=profile
    )
    assert OutreachViolationCode.INVENTED_RECRUITER_NAME in _codes(invented_result)

    observed_result = validate_outreach_message(
        draft=invented,
        analysis=analysis,
        job=job,
        profile=profile,
        recruiter_name="李经理",
    )
    assert OutreachViolationCode.INVENTED_RECRUITER_NAME not in _codes(observed_result)
    prefixed_result = validate_outreach_message(
        draft=invented.model_copy(update={"salutation": "张李经理您好"}),
        analysis=analysis,
        job=job,
        profile=profile,
        recruiter_name="李经理",
    )
    assert OutreachViolationCode.INVENTED_RECRUITER_NAME in _codes(prefixed_result)

    english = message.model_copy(update={"closing": "Thanks"})
    english_result = validate_outreach_message(
        draft=english, analysis=analysis, job=job, profile=profile
    )
    assert OutreachViolationCode.NON_CHINESE_CONTENT in _codes(english_result)

    policy_result = validate_outreach_message(
        draft=message,
        analysis=analysis,
        job=job,
        profile=profile,
        policy=OutreachChannelPolicy(
            channel=OutreachChannel.BOSS, max_message_chars=10, max_claims=3
        ),
    )
    assert OutreachViolationCode.CHANNEL_POLICY_VIOLATION in _codes(policy_result)


def test_analysis_match_without_evidence_cannot_authorize_claim(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, profile, analysis = build_outreach_scope(jobintel_repo)
    first = analysis.requirement_matches[0]
    empty_match = RequirementMatchDraft.model_construct(
        requirement_id=first.requirement_id,
        status=MatchStatus.MATCHED,
        evidence_ids=(),
        confidence=first.confidence,
        reason=first.reason,
    )
    unsafe_analysis = analysis.model_copy(
        update={"requirement_matches": (empty_match, *analysis.requirement_matches[1:])}
    )
    result = validate_outreach_message(
        draft=valid_outreach_message(job),
        analysis=unsafe_analysis,
        job=job,
        profile=profile,
    )
    assert _codes(result) >= {
        OutreachViolationCode.REQUIREMENT_WITHOUT_CLAIM_EVIDENCE,
        OutreachViolationCode.EVIDENCE_NOT_MATCHED_TO_REQUIREMENT,
    }
