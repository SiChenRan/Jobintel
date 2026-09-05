"""Deterministic evidence and content guardrails for HR outreach drafts."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field

from jobintel.models import (
    CandidateProfile,
    FrozenDomainModel,
    JobAnalysis,
    JobPosting,
    MatchStatus,
    NonEmptyStr,
)
from jobintel.outreach.models import OutreachMessageDraft
from jobintel.outreach.policy import (
    BOSS_DRAFT_POLICY,
    OutreachChannelPolicy,
    OutreachChannelPolicyError,
    render_outreach_message,
)

OUTREACH_GUARDRAIL_VERSION = "jobintel-outreach-guardrail-v1"

_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_GENERIC_SALUTATIONS = frozenset(
    {
        "您好",
        "hr您好",
        "招聘负责人您好",
        "您好, 招聘负责人",
    }
)
_PARTIAL_OVERSTATEMENTS = ("精通", "专家", "完全胜任", "完美匹配", "百分之百匹配")


class OutreachViolationCode(StrEnum):
    """Stable machine-readable outreach rejection codes."""

    ANALYSIS_JOB_SCOPE_MISMATCH = "analysis_job_scope_mismatch"
    ANALYSIS_PROFILE_SCOPE_MISMATCH = "analysis_profile_scope_mismatch"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    REQUIREMENT_NOT_POSITIVE = "requirement_not_positive"
    REQUIREMENT_WITHOUT_CLAIM_EVIDENCE = "requirement_without_claim_evidence"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    EVIDENCE_NOT_MATCHED_TO_REQUIREMENT = "evidence_not_matched_to_requirement"
    INVENTED_RECRUITER_NAME = "invented_recruiter_name"
    NON_CHINESE_CONTENT = "non_chinese_content"
    PARTIAL_MATCH_OVERSTATED = "partial_match_overstated"
    CHANNEL_POLICY_VIOLATION = "channel_policy_violation"


class OutreachGuardrailViolation(FrozenDomainModel):
    """Content-minimal explanation of one rejected outreach field."""

    code: OutreachViolationCode
    field_path: NonEmptyStr
    message: NonEmptyStr
    requirement_id: NonEmptyStr | None = None
    evidence_id: NonEmptyStr | None = None


class OutreachGuardrailResult(FrozenDomainModel):
    """All deterministic violations found during one validation pass."""

    violations: tuple[OutreachGuardrailViolation, ...] = ()
    guardrail_version: NonEmptyStr = Field(default=OUTREACH_GUARDRAIL_VERSION)

    @property
    def is_valid(self) -> bool:
        """Return whether the draft passed every deterministic rule."""
        return not self.violations


def _violation(
    code: OutreachViolationCode,
    field_path: str,
    message: str,
    *,
    requirement_id: str | None = None,
    evidence_id: str | None = None,
) -> OutreachGuardrailViolation:
    """Build one consistently shaped violation."""
    return OutreachGuardrailViolation(
        code=code,
        field_path=field_path,
        message=message,
        requirement_id=requirement_id,
        evidence_id=evidence_id,
    )


def _normalized_salutation(value: str) -> str:
    """Normalize harmless punctuation and casing for salutation checks."""
    return re.sub(r"[\s,.!:\u3002\uFF01\uFF0C\uFF1A]+", "", value).casefold()


def validate_outreach_message(
    *,
    draft: OutreachMessageDraft,
    analysis: JobAnalysis,
    job: JobPosting,
    profile: CandidateProfile,
    recruiter_name: str | None = None,
    policy: OutreachChannelPolicy = BOSS_DRAFT_POLICY,
) -> OutreachGuardrailResult:
    """Validate draft scope, grounded claims, salutation, language, and limits."""
    violations: list[OutreachGuardrailViolation] = []
    if (analysis.job_id, analysis.job_version) != (job.job_id, job.job_version):
        violations.append(
            _violation(
                OutreachViolationCode.ANALYSIS_JOB_SCOPE_MISMATCH,
                "analysis_id",
                "analysis does not belong to the supplied job version",
            )
        )
    if (analysis.candidate_id, analysis.profile_version) != (
        profile.candidate_id,
        profile.profile_version,
    ):
        violations.append(
            _violation(
                OutreachViolationCode.ANALYSIS_PROFILE_SCOPE_MISMATCH,
                "analysis_id",
                "analysis does not belong to the supplied candidate profile version",
            )
        )

    match_by_requirement = {match.requirement_id: match for match in analysis.requirement_matches}
    requirement_ids = {requirement.requirement_id for requirement in job.requirements}
    profile_evidence_ids = {evidence.evidence_id for evidence in profile.evidence}

    for claim_index, claim in enumerate(draft.claims):
        claim_path = f"claims.{claim_index}"
        permitted_evidence: set[str] = set()
        partial_requirement_ids: list[str] = []
        for requirement_index, requirement_id in enumerate(claim.requirement_ids):
            path = f"{claim_path}.requirement_ids.{requirement_index}"
            if requirement_id not in requirement_ids:
                violations.append(
                    _violation(
                        OutreachViolationCode.UNKNOWN_REQUIREMENT,
                        path,
                        "claim references a requirement outside the analyzed job version",
                        requirement_id=requirement_id,
                    )
                )
                continue
            match = match_by_requirement.get(requirement_id)
            if match is None or match.status is MatchStatus.MISSING:
                violations.append(
                    _violation(
                        OutreachViolationCode.REQUIREMENT_NOT_POSITIVE,
                        path,
                        "claim may reference only matched or partial requirements",
                        requirement_id=requirement_id,
                    )
                )
                continue
            permitted_evidence.update(match.evidence_ids)
            if not set(claim.evidence_ids).intersection(match.evidence_ids):
                violations.append(
                    _violation(
                        OutreachViolationCode.REQUIREMENT_WITHOUT_CLAIM_EVIDENCE,
                        f"{claim_path}.evidence_ids",
                        "each referenced requirement needs one of its matched evidence items",
                        requirement_id=requirement_id,
                    )
                )
            if match.status is MatchStatus.PARTIAL:
                partial_requirement_ids.append(requirement_id)

        for evidence_index, evidence_id in enumerate(claim.evidence_ids):
            path = f"{claim_path}.evidence_ids.{evidence_index}"
            if evidence_id not in profile_evidence_ids:
                violations.append(
                    _violation(
                        OutreachViolationCode.UNKNOWN_EVIDENCE,
                        path,
                        "claim evidence does not belong to the analyzed candidate profile version",
                        evidence_id=evidence_id,
                    )
                )
            elif evidence_id not in permitted_evidence:
                violations.append(
                    _violation(
                        OutreachViolationCode.EVIDENCE_NOT_MATCHED_TO_REQUIREMENT,
                        path,
                        "claim evidence was not cited by any referenced requirement match",
                        evidence_id=evidence_id,
                    )
                )

        if partial_requirement_ids and any(
            phrase in claim.text for phrase in _PARTIAL_OVERSTATEMENTS
        ):
            violations.append(
                _violation(
                    OutreachViolationCode.PARTIAL_MATCH_OVERSTATED,
                    f"{claim_path}.text",
                    "partial requirement wording overstates the available evidence",
                    requirement_id=partial_requirement_ids[0],
                )
            )

    normalized_salutation = _normalized_salutation(draft.salutation)
    normalized_generics = {_normalized_salutation(item) for item in _GENERIC_SALUTATIONS}
    normalized_recruiter = _normalized_salutation(recruiter_name or "")
    personalized_salutations = (
        {f"{normalized_recruiter}您好", f"您好{normalized_recruiter}"}
        if normalized_recruiter
        else set()
    )
    if normalized_salutation not in normalized_generics | personalized_salutations:
        violations.append(
            _violation(
                OutreachViolationCode.INVENTED_RECRUITER_NAME,
                "salutation",
                "salutation must be generic or contain the observed recruiter name",
            )
        )

    language_fields = (
        ("salutation", draft.salutation),
        ("motivation", draft.motivation),
        *((f"claims.{index}.text", claim.text) for index, claim in enumerate(draft.claims)),
        ("conversation_opener", draft.conversation_opener),
        ("closing", draft.closing),
    )
    for field_path, value in language_fields:
        if not _CJK_PATTERN.search(value):
            violations.append(
                _violation(
                    OutreachViolationCode.NON_CHINESE_CONTENT,
                    field_path,
                    "user-facing outreach text must contain Simplified Chinese",
                )
            )

    try:
        render_outreach_message(draft, policy=policy)
    except OutreachChannelPolicyError:
        violations.append(
            _violation(
                OutreachViolationCode.CHANNEL_POLICY_VIOLATION,
                "rendered_message",
                "draft exceeds the configured local channel policy",
            )
        )

    return OutreachGuardrailResult(violations=tuple(violations))
