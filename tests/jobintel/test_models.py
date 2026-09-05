"""Tests for strict JobIntel domain contracts and stable identities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceType,
    GroundedClaim,
    JobAnalysis,
    JobAnalysisDraft,
    JobPosting,
    JobRequirement,
    MatchStatus,
    Recommendation,
    RequirementCategory,
    RequirementImportance,
    RequirementMatchDraft,
    ScoreBreakdown,
    ScoreGroupBreakdown,
    canonicalize_requirement_text,
    stable_requirement_id,
)

_SHA256 = "a" * 64
_NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def _requirement(
    requirement_id: str = "req-python",
    *,
    source_order: int = 0,
    importance: RequirementImportance = RequirementImportance.MUST,
) -> JobRequirement:
    return JobRequirement(
        requirement_id=requirement_id,
        text="Python production experience",
        category=RequirementCategory.EXPERIENCE,
        importance=importance,
        normalized_skill="Python",
        source_order=source_order,
    )


def _evidence(evidence_id: str = "ev-python", *, source_order: int = 0) -> CandidateEvidence:
    return CandidateEvidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.EXPERIENCE,
        title="Backend Engineer",
        content="Built Python services for three years.",
        skills=("Python", "FastAPI"),
        source_order=source_order,
    )


def _match(
    requirement_id: str = "req-python", status: MatchStatus = MatchStatus.MATCHED
) -> RequirementMatchDraft:
    evidence_ids = () if status is MatchStatus.MISSING else ("ev-python",)
    return RequirementMatchDraft(
        requirement_id=requirement_id,
        status=status,
        evidence_ids=evidence_ids,
        confidence=0.9,
        reason="Candidate evidence supports the requirement.",
    )


def test_job_and_profile_are_strict_immutable_versioned_models() -> None:
    requirement = _requirement()
    job = JobPosting(
        job_id="  J001  ",
        job_version=1,
        company_name="Example Co",
        title="Backend Engineer",
        description="Build Python services.",
        requirements=(requirement,),
        source_sha256=_SHA256,
        created_at=_NOW,
    )
    profile = CandidateProfile(
        candidate_id="C001",
        profile_version=2,
        summary="Backend developer",
        evidence=(_evidence(),),
        source_sha256=_SHA256,
        created_at=_NOW,
    )

    assert job.job_id == "J001"
    assert job.requirements == (requirement,)
    assert profile.evidence[0].skills == ("Python", "FastAPI")
    with pytest.raises(ValidationError, match="frozen"):
        job.title = "Mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        JobRequirement.model_validate({**requirement.model_dump(), "model_score": 100})


def test_models_reject_invalid_versions_hashes_and_duplicate_children() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        JobPosting(
            job_id="J001",
            job_version=0,
            company_name="Example",
            title="Engineer",
            description="Description",
            source_sha256=_SHA256,
            created_at=_NOW,
        )
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        CandidateProfile(
            candidate_id="C001",
            profile_version=1,
            source_sha256="NOT-A-HASH",
            created_at=_NOW,
        )
    with pytest.raises(ValidationError, match="duplicate requirement_id"):
        JobPosting(
            job_id="J001",
            job_version=1,
            company_name="Example",
            title="Engineer",
            description="Description",
            requirements=(_requirement(), _requirement(source_order=1)),
            source_sha256=_SHA256,
            created_at=_NOW,
        )
    with pytest.raises(ValidationError, match="duplicate evidence_id"):
        CandidateProfile(
            candidate_id="C001",
            profile_version=1,
            evidence=(_evidence(), _evidence(source_order=1)),
            source_sha256=_SHA256,
            created_at=_NOW,
        )


def test_models_require_aware_datetimes_and_normalize_them_to_utc() -> None:
    offset_time = datetime(2026, 9, 4, 16, 0, tzinfo=timezone(timedelta(hours=8)))
    profile = CandidateProfile(
        candidate_id="C001",
        profile_version=1,
        source_sha256=_SHA256,
        created_at=offset_time,
    )
    assert profile.created_at == _NOW
    assert profile.created_at.tzinfo is UTC

    with pytest.raises(ValidationError, match="timezone_aware"):
        CandidateProfile(
            candidate_id="C001",
            profile_version=1,
            source_sha256=_SHA256,
            created_at=datetime(2026, 9, 4, 8, 0),
        )


@pytest.mark.parametrize("status", [MatchStatus.MATCHED, MatchStatus.PARTIAL])
def test_positive_matches_require_evidence(status: MatchStatus) -> None:
    with pytest.raises(ValidationError, match="requires at least one evidence_id"):
        RequirementMatchDraft(
            requirement_id="req-python",
            status=status,
            evidence_ids=(),
            confidence=0.5,
            reason="Some reason",
        )


def test_missing_match_rejects_evidence_and_match_fields_are_bounded() -> None:
    with pytest.raises(ValidationError, match="must not contain evidence_ids"):
        RequirementMatchDraft(
            requirement_id="req-python",
            status=MatchStatus.MISSING,
            evidence_ids=("ev-python",),
            confidence=0.5,
            reason="No support",
        )
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        RequirementMatchDraft(
            requirement_id="req-python",
            status=MatchStatus.MISSING,
            confidence=1.01,
            reason="No support",
        )
    with pytest.raises(ValidationError, match="duplicate evidence_id"):
        RequirementMatchDraft(
            requirement_id="req-python",
            status=MatchStatus.MATCHED,
            evidence_ids=("ev-python", "ev-python"),
            confidence=1,
            reason="Supported",
        )


def test_grounded_claim_requires_unique_nonempty_references() -> None:
    claim = GroundedClaim(
        claim_id="claim-1",
        text="Strong Python background",
        requirement_ids=("req-python",),
        evidence_ids=("ev-python",),
    )
    assert claim.requirement_ids == ("req-python",)
    with pytest.raises(ValidationError, match="at least 1 item"):
        GroundedClaim(
            claim_id="claim-2",
            text="Unsupported",
            requirement_ids=(),
            evidence_ids=("ev-python",),
        )


def test_draft_excludes_program_controlled_fields_and_duplicate_matches() -> None:
    payload = {
        "job_id": "J001",
        "job_version": 1,
        "candidate_id": "C001",
        "profile_version": 2,
        "requirement_matches": [_match().model_dump(mode="json")],
        "next_action": "Apply after tailoring the resume.",
    }
    draft = JobAnalysisDraft.model_validate(payload)
    assert draft.requirement_matches[0].status is MatchStatus.MATCHED

    with pytest.raises(ValidationError, match="Extra inputs"):
        JobAnalysisDraft.model_validate({**payload, "score": 100})
    with pytest.raises(ValidationError, match="duplicate matched requirement_id"):
        JobAnalysisDraft.model_validate(
            {**payload, "requirement_matches": [payload["requirement_matches"][0]] * 2}
        )


def test_final_analysis_requires_program_metadata_and_consistent_score() -> None:
    group = ScoreGroupBreakdown(
        importance=RequirementImportance.MUST,
        requirement_count=1,
        status_total=Decimal("1"),
        status_mean=Decimal("1"),
        base_weight=Decimal("0.60"),
        normalized_weight=Decimal("1"),
        weighted_contribution=Decimal("1"),
    )
    breakdown = ScoreBreakdown(groups=(group,), raw_score=Decimal("100"), score=100)
    payload = {
        "job_id": "J001",
        "job_version": 1,
        "candidate_id": "C001",
        "profile_version": 2,
        "requirement_matches": (_match(),),
        "next_action": "Apply.",
        "analysis_id": "analysis-1",
        "run_id": "run-1",
        "score": 100,
        "recommendation": Recommendation.STRONG_APPLY,
        "score_breakdown": breakdown,
        "prompt_version": "prompt-v1",
        "parser_version": "parser-v1",
        "toolset_version": "toolset-v1",
        "scoring_version": "scoring-v1",
        "schema_version": "schema-v1",
        "provenance_version": "provenance-v1",
        "provenance_digest": _SHA256,
        "created_at": _NOW,
    }
    analysis = JobAnalysis.model_validate(payload)
    assert analysis.score == 100

    with pytest.raises(ValidationError, match="score must equal"):
        JobAnalysis.model_validate({**payload, "score": 99})


def test_stable_requirement_id_canonicalizes_text_and_scopes_identity() -> None:
    first = stable_requirement_id(
        job_id="J001",
        job_version=1,
        text=" Python\u3000and   SQL ",
        category=RequirementCategory.SKILL,
    )
    equivalent = stable_requirement_id(
        job_id="J001",
        job_version=1,
        text="python and sql",
        category=RequirementCategory.SKILL,
    )
    next_job_version = stable_requirement_id(
        job_id="J001",
        job_version=2,
        text="python and sql",
        category=RequirementCategory.SKILL,
    )
    duplicate = stable_requirement_id(
        job_id="J001",
        job_version=1,
        text="python and sql",
        category=RequirementCategory.SKILL,
        duplicate_ordinal=2,
    )

    assert canonicalize_requirement_text(" Python\u3000and   SQL ") == "python and sql"
    assert first == equivalent
    assert first.startswith("req_") and len(first) == 28
    assert len({first, next_job_version, duplicate}) == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"job_id": " "}, "job_id"),
        ({"job_version": 0}, "job_version"),
        ({"text": " \t "}, "requirement text"),
        ({"duplicate_ordinal": 0}, "duplicate_ordinal"),
    ],
)
def test_stable_requirement_id_rejects_invalid_components(
    kwargs: dict[str, object], message: str
) -> None:
    inputs: dict[str, object] = {
        "job_id": "J001",
        "job_version": 1,
        "text": "Python",
        "category": RequirementCategory.SKILL,
    }
    inputs.update(kwargs)
    with pytest.raises(ValueError, match=message):
        stable_requirement_id(**inputs)  # type: ignore[arg-type]
