"""Pure deterministic provenance guardrail for JobIntel analysis drafts."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import Field

from jobintel.models import (
    CandidateProfile,
    FrozenDomainModel,
    JobAnalysisDraft,
    JobPosting,
    MatchStatus,
    NonEmptyStr,
    RequirementMatchDraft,
)
from jobintel.provenance import (
    EntityKind,
    EntityRef,
    EvidenceSearchScope,
    ProvenanceLedger,
    evidence_content_sha256,
)

GUARDRAIL_VERSION = "jobintel-guardrail-v1"


class ViolationCode(StrEnum):
    """Stable machine-readable codes returned to repair and evaluation paths."""

    DRAFT_JOB_SCOPE_MISMATCH = "draft_job_scope_mismatch"
    DRAFT_PROFILE_SCOPE_MISMATCH = "draft_profile_scope_mismatch"
    JOB_NOT_OBSERVED = "job_not_observed"
    PROFILE_NOT_OBSERVED = "profile_not_observed"
    MISSING_REQUIREMENT_MATCH = "missing_requirement_match"
    DUPLICATE_REQUIREMENT_MATCH = "duplicate_requirement_match"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    MISSING_EVIDENCE_SEARCH = "missing_evidence_search"
    POSITIVE_MATCH_WITHOUT_EVIDENCE = "positive_match_without_evidence"
    MISSING_MATCH_WITH_EVIDENCE = "missing_match_with_evidence"
    EVIDENCE_NOT_RETURNED = "evidence_not_returned"
    EVIDENCE_SCOPE_MISMATCH = "evidence_scope_mismatch"
    EVIDENCE_NOT_IN_PROFILE = "evidence_not_in_profile"
    EVIDENCE_CONTENT_MISMATCH = "evidence_content_mismatch"
    STRENGTH_REQUIRES_MATCHED_REQUIREMENT = "strength_requires_matched_requirement"
    STRENGTH_EVIDENCE_MISMATCH = "strength_evidence_mismatch"
    UNKNOWN_NARRATIVE_REQUIREMENT = "unknown_narrative_requirement"


class GuardrailViolation(FrozenDomainModel):
    """Content-free, structured explanation of one rejected draft field."""

    code: ViolationCode
    field_path: NonEmptyStr
    message: NonEmptyStr
    requirement_id: NonEmptyStr | None = None
    evidence_id: NonEmptyStr | None = None


class GuardrailResult(FrozenDomainModel):
    """All deterministic violations found during one validation pass."""

    violations: tuple[GuardrailViolation, ...] = ()
    guardrail_version: NonEmptyStr = Field(default=GUARDRAIL_VERSION)

    @property
    def is_valid(self) -> bool:
        """Return whether the draft passed every deterministic rule."""
        return not self.violations


def _violation(
    code: ViolationCode,
    field_path: str,
    message: str,
    *,
    requirement_id: str | None = None,
    evidence_id: str | None = None,
) -> GuardrailViolation:
    """Build a consistently shaped guardrail violation."""
    return GuardrailViolation(
        code=code,
        field_path=field_path,
        message=message,
        requirement_id=requirement_id,
        evidence_id=evidence_id,
    )


def validate_analysis_draft(
    *,
    draft: JobAnalysisDraft,
    job: JobPosting,
    profile: CandidateProfile,
    ledger: ProvenanceLedger,
) -> GuardrailResult:
    """Validate draft identities and citations against this run's observations."""
    violations: list[GuardrailViolation] = []
    expected_job = (job.job_id, job.job_version)
    draft_job = (draft.job_id, draft.job_version)
    if draft_job != expected_job:
        violations.append(
            _violation(
                ViolationCode.DRAFT_JOB_SCOPE_MISMATCH,
                "job_id",
                "draft job identity differs from the run job version",
            )
        )
    expected_profile = (profile.candidate_id, profile.profile_version)
    draft_profile = (draft.candidate_id, draft.profile_version)
    if draft_profile != expected_profile:
        violations.append(
            _violation(
                ViolationCode.DRAFT_PROFILE_SCOPE_MISMATCH,
                "candidate_id",
                "draft candidate identity differs from the run profile version",
            )
        )

    if not ledger.has_entity(
        EntityRef(kind=EntityKind.JOB, entity_id=job.job_id, version=job.job_version)
    ):
        violations.append(
            _violation(
                ViolationCode.JOB_NOT_OBSERVED,
                "job_id",
                "job version was not returned by a successful run observation",
            )
        )
    if not ledger.has_entity(
        EntityRef(
            kind=EntityKind.CANDIDATE_PROFILE,
            entity_id=profile.candidate_id,
            version=profile.profile_version,
        )
    ):
        violations.append(
            _violation(
                ViolationCode.PROFILE_NOT_OBSERVED,
                "candidate_id",
                "profile version was not returned by a successful run observation",
            )
        )

    requirement_by_id = {item.requirement_id: item for item in job.requirements}
    match_counts = Counter(item.requirement_id for item in draft.requirement_matches)
    match_by_id: dict[str, RequirementMatchDraft] = {}
    for index, match in enumerate(draft.requirement_matches):
        path = f"requirement_matches.{index}"
        if match.requirement_id not in requirement_by_id:
            violations.append(
                _violation(
                    ViolationCode.UNKNOWN_REQUIREMENT,
                    f"{path}.requirement_id",
                    "match references a requirement outside the run job version",
                    requirement_id=match.requirement_id,
                )
            )
            continue
        match_by_id.setdefault(match.requirement_id, match)

    for requirement in job.requirements:
        requirement_id = requirement.requirement_id
        count = match_counts[requirement_id]
        if count == 0:
            violations.append(
                _violation(
                    ViolationCode.MISSING_REQUIREMENT_MATCH,
                    "requirement_matches",
                    "job requirement has no draft match",
                    requirement_id=requirement_id,
                )
            )
        elif count > 1:
            violations.append(
                _violation(
                    ViolationCode.DUPLICATE_REQUIREMENT_MATCH,
                    "requirement_matches",
                    "job requirement has more than one draft match",
                    requirement_id=requirement_id,
                )
            )

        scope = EvidenceSearchScope(
            job_id=job.job_id,
            job_version=job.job_version,
            requirement_id=requirement_id,
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
        )
        if not ledger.has_successful_search(scope):
            violations.append(
                _violation(
                    ViolationCode.MISSING_EVIDENCE_SEARCH,
                    "requirement_matches",
                    "requirement lacks a successful evidence search in this run",
                    requirement_id=requirement_id,
                )
            )

    profile_evidence = {item.evidence_id: item for item in profile.evidence}
    for index, match in enumerate(draft.requirement_matches):
        if match.requirement_id not in requirement_by_id:
            continue
        path = f"requirement_matches.{index}"
        if match.status in (MatchStatus.MATCHED, MatchStatus.PARTIAL) and not match.evidence_ids:
            violations.append(
                _violation(
                    ViolationCode.POSITIVE_MATCH_WITHOUT_EVIDENCE,
                    f"{path}.evidence_ids",
                    "matched or partial result requires evidence",
                    requirement_id=match.requirement_id,
                )
            )
        if match.status is MatchStatus.MISSING and match.evidence_ids:
            violations.append(
                _violation(
                    ViolationCode.MISSING_MATCH_WITH_EVIDENCE,
                    f"{path}.evidence_ids",
                    "missing result must not cite evidence",
                    requirement_id=match.requirement_id,
                )
            )

        scope = EvidenceSearchScope(
            job_id=job.job_id,
            job_version=job.job_version,
            requirement_id=match.requirement_id,
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
        )
        receipt_by_id = {
            receipt.evidence_id: receipt for receipt in ledger.receipts_for_scope(scope)
        }
        for evidence_index, evidence_id in enumerate(match.evidence_ids):
            evidence_path = f"{path}.evidence_ids.{evidence_index}"
            receipt = receipt_by_id.get(evidence_id)
            if receipt is None:
                other_receipts = ledger.receipts_for_evidence(evidence_id)
                code = (
                    ViolationCode.EVIDENCE_SCOPE_MISMATCH
                    if other_receipts
                    else ViolationCode.EVIDENCE_NOT_RETURNED
                )
                violations.append(
                    _violation(
                        code,
                        evidence_path,
                        "evidence was not returned for this exact requirement search scope",
                        requirement_id=match.requirement_id,
                        evidence_id=evidence_id,
                    )
                )
                continue
            evidence = profile_evidence.get(evidence_id)
            if evidence is None:
                violations.append(
                    _violation(
                        ViolationCode.EVIDENCE_NOT_IN_PROFILE,
                        evidence_path,
                        "evidence does not belong to the run candidate profile version",
                        requirement_id=match.requirement_id,
                        evidence_id=evidence_id,
                    )
                )
            elif receipt.content_sha256 != evidence_content_sha256(evidence):
                violations.append(
                    _violation(
                        ViolationCode.EVIDENCE_CONTENT_MISMATCH,
                        evidence_path,
                        "evidence content hash differs from the run profile version",
                        requirement_id=match.requirement_id,
                        evidence_id=evidence_id,
                    )
                )

    for index, strength in enumerate(draft.strengths):
        allowed_evidence: set[str] = set()
        for requirement_id in strength.requirement_ids:
            strength_match = match_by_id.get(requirement_id)
            if strength_match is None or strength_match.status is not MatchStatus.MATCHED:
                violations.append(
                    _violation(
                        ViolationCode.STRENGTH_REQUIRES_MATCHED_REQUIREMENT,
                        f"strengths.{index}.requirement_ids",
                        "strength may reference only matched requirements",
                        requirement_id=requirement_id,
                    )
                )
            else:
                allowed_evidence.update(strength_match.evidence_ids)
        for evidence_id in strength.evidence_ids:
            if evidence_id not in allowed_evidence:
                violations.append(
                    _violation(
                        ViolationCode.STRENGTH_EVIDENCE_MISMATCH,
                        f"strengths.{index}.evidence_ids",
                        "strength evidence must be a subset of its match evidence",
                        evidence_id=evidence_id,
                    )
                )

    for field_name, requirement_ids in (
        (
            "resume_suggestions",
            tuple(item.requirement_id for item in draft.resume_suggestions),
        ),
        (
            "interview_topics",
            tuple(item.requirement_id for item in draft.interview_topics),
        ),
    ):
        for index, requirement_id in enumerate(requirement_ids):
            if requirement_id not in requirement_by_id:
                violations.append(
                    _violation(
                        ViolationCode.UNKNOWN_NARRATIVE_REQUIREMENT,
                        f"{field_name}.{index}.requirement_id",
                        "narrative item references an unknown job requirement",
                        requirement_id=requirement_id,
                    )
                )

    return GuardrailResult(violations=tuple(violations))
