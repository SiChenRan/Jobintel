"""Strict domain contracts for JobIntel V1.

The language model may construct :class:`JobAnalysisDraft`, while final scores,
recommendations, identifiers, timestamps, and version metadata only exist on
the program-authored :class:`JobAnalysis` type.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

REQUIREMENT_ID_VERSION = "requirement-id-v1"
ANALYSIS_SCHEMA_VERSION = "jobintel-analysis-v2"


def _to_utc(value: datetime) -> datetime:
    """Normalize an already-aware datetime to UTC."""
    return value.astimezone(UTC)


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
UtcDateTime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
DecimalValue = Decimal


class FrozenDomainModel(BaseModel):
    """Base model for immutable values that cross module boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RequirementCategory(StrEnum):
    """Semantic category assigned to a job requirement."""

    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    PROJECT = "project"
    LANGUAGE = "language"
    OTHER = "other"


class RequirementImportance(StrEnum):
    """Importance group used by the deterministic scoring policy."""

    MUST = "must"
    PREFERRED = "preferred"
    BONUS = "bonus"


class EvidenceType(StrEnum):
    """Kind of candidate evidence stored in a profile version."""

    EDUCATION = "education"
    EXPERIENCE = "experience"
    PROJECT = "project"
    SKILL = "skill"
    CERTIFICATE = "certificate"
    OTHER = "other"


class MatchStatus(StrEnum):
    """Candidate coverage of one job requirement."""

    MATCHED = "matched"
    PARTIAL = "partial"
    MISSING = "missing"


class EvidenceMatchMethod(StrEnum):
    """Highest-priority deterministic strategy that selected an evidence hit."""

    EXACT = "exact"
    ALIAS = "alias"
    LEXICAL = "lexical"
    FUZZY = "fuzzy"


class Recommendation(StrEnum):
    """Program-derived application recommendation."""

    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    LOW_PRIORITY = "low_priority"
    SKIP = "skip"


class Company(FrozenDomainModel):
    """Company context optionally associated with a job posting."""

    company_id: NonEmptyStr
    name: NonEmptyStr
    industry: NonEmptyStr | None = None
    headquarters: NonEmptyStr | None = None
    website: NonEmptyStr | None = None
    description: NonEmptyStr | None = None


def _duplicate_values(values: tuple[str, ...]) -> list[str]:
    """Return duplicate strings in deterministic order."""
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _require_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Reject duplicate identifiers while preserving tuple order."""
    duplicates = _duplicate_values(values)
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")
    return values


def canonicalize_requirement_text(text: str) -> str:
    """Canonicalize requirement text for stable identity generation.

    Unicode compatibility forms, casing, and runs of whitespace do not affect
    identity. Punctuation remains significant because it can change meaning.

    Args:
        text: Human-readable requirement text.

    Returns:
        Canonical text suitable for hashing.

    Raises:
        ValueError: If the normalized text is empty.
    """
    canonical = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    if not canonical:
        raise ValueError("requirement text must not be empty")
    return canonical


def stable_requirement_id(
    *,
    job_id: str,
    job_version: int,
    text: str,
    category: RequirementCategory,
    duplicate_ordinal: int = 1,
) -> str:
    """Build a deterministic requirement ID from program-controlled inputs.

    Args:
        job_id: Stable job identity.
        job_version: Immutable job revision, starting at one.
        text: Requirement text before canonicalization.
        category: Parsed semantic category.
        duplicate_ordinal: One-based occurrence among identical requirements.

    Returns:
        A namespaced ID containing a truncated SHA-256 digest.

    Raises:
        ValueError: If an identity component is invalid.
    """
    normalized_job_id = job_id.strip()
    if not normalized_job_id:
        raise ValueError("job_id must not be empty")
    if job_version < 1:
        raise ValueError("job_version must be at least 1")
    if duplicate_ordinal < 1:
        raise ValueError("duplicate_ordinal must be at least 1")

    payload = {
        "category": category.value,
        "duplicate_ordinal": duplicate_ordinal,
        "job_id": normalized_job_id,
        "job_version": job_version,
        "requirement_text": canonicalize_requirement_text(text),
        "version": REQUIREMENT_ID_VERSION,
    }
    canonical_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    return f"req_{digest[:24]}"


class JobRequirement(FrozenDomainModel):
    """One program-identified requirement extracted from a job posting."""

    requirement_id: NonEmptyStr
    text: NonEmptyStr
    category: RequirementCategory
    importance: RequirementImportance
    normalized_skill: NonEmptyStr | None = None
    source_order: int = Field(ge=0)


class JobPosting(FrozenDomainModel):
    """Immutable version of a job posting and its parsed requirements."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    company_id: NonEmptyStr | None = None
    company_name: NonEmptyStr
    title: NonEmptyStr
    location: NonEmptyStr | None = None
    employment_type: NonEmptyStr | None = None
    description: NonEmptyStr
    requirements: tuple[JobRequirement, ...] = ()
    source_url: NonEmptyStr | None = None
    source_sha256: Sha256Hex
    created_at: UtcDateTime

    @field_validator("requirements")
    @classmethod
    def validate_requirements(
        cls, requirements: tuple[JobRequirement, ...]
    ) -> tuple[JobRequirement, ...]:
        """Require unique IDs and source positions within a job version."""
        _require_unique(tuple(item.requirement_id for item in requirements), "requirement_id")
        source_orders = tuple(str(item.source_order) for item in requirements)
        _require_unique(source_orders, "requirement source_order")
        return requirements


class CandidateEvidence(FrozenDomainModel):
    """One citable evidence item in an immutable candidate profile."""

    evidence_id: NonEmptyStr
    evidence_type: EvidenceType
    title: NonEmptyStr
    content: NonEmptyStr
    skills: tuple[NonEmptyStr, ...] = ()
    source_order: int = Field(ge=0)

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, skills: tuple[str, ...]) -> tuple[str, ...]:
        """Reject repeated skills in one evidence item."""
        return _require_unique(skills, "skill")


class CandidateProfile(FrozenDomainModel):
    """Immutable candidate profile version containing citable evidence."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    summary: NonEmptyStr | None = None
    evidence: tuple[CandidateEvidence, ...] = ()
    source_sha256: Sha256Hex
    created_at: UtcDateTime

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls, evidence: tuple[CandidateEvidence, ...]
    ) -> tuple[CandidateEvidence, ...]:
        """Require unique evidence IDs and source positions within a profile."""
        _require_unique(tuple(item.evidence_id for item in evidence), "evidence_id")
        source_orders = tuple(str(item.source_order) for item in evidence)
        _require_unique(source_orders, "evidence source_order")
        return evidence


class EvidenceSearchHit(FrozenDomainModel):
    """One deterministically ranked candidate evidence search result."""

    evidence: CandidateEvidence
    match_method: EvidenceMatchMethod
    relevance_score: float = Field(ge=0, le=1)
    matched_terms: tuple[NonEmptyStr, ...] = ()

    @field_validator("matched_terms")
    @classmethod
    def validate_matched_terms(cls, terms: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate normalized terms in one search hit."""
        return _require_unique(terms, "matched search term")


class SearchCandidateEvidenceOutput(FrozenDomainModel):
    """Scoped, deterministic output of candidate evidence search."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    requirement_id: NonEmptyStr
    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    query: NonEmptyStr
    hits: tuple[EvidenceSearchHit, ...] = ()

    @field_validator("hits")
    @classmethod
    def validate_hits(cls, hits: tuple[EvidenceSearchHit, ...]) -> tuple[EvidenceSearchHit, ...]:
        """Return each evidence identity at most once."""
        _require_unique(tuple(hit.evidence.evidence_id for hit in hits), "search evidence_id")
        return hits


class RequirementMatchDraft(FrozenDomainModel):
    """Model-authored assessment of one requirement."""

    requirement_id: NonEmptyStr
    status: MatchStatus
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    confidence: float = Field(ge=0, le=1)
    reason: NonEmptyStr

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, evidence_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Reject repeated citations within one match."""
        return _require_unique(evidence_ids, "evidence_id")

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        """Enforce evidence rules implied by the match status."""
        if self.status in (MatchStatus.MATCHED, MatchStatus.PARTIAL) and not self.evidence_ids:
            raise ValueError(f"{self.status.value} match requires at least one evidence_id")
        if self.status is MatchStatus.MISSING and self.evidence_ids:
            raise ValueError("missing match must not contain evidence_ids")
        return self


class GroundedClaim(FrozenDomainModel):
    """A narrative strength linked to requirements and candidate evidence."""

    claim_id: NonEmptyStr
    text: NonEmptyStr
    requirement_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("requirement_ids", "evidence_ids")
    @classmethod
    def validate_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate references in a grounded claim."""
        return _require_unique(values, "claim reference")


class ResumeSuggestion(FrozenDomainModel):
    """Resume improvement associated with one real requirement."""

    requirement_id: NonEmptyStr
    text: NonEmptyStr


class InterviewTopic(FrozenDomainModel):
    """Interview preparation topic associated with one real requirement."""

    requirement_id: NonEmptyStr
    text: NonEmptyStr


class MissingSkill(FrozenDomainModel):
    """Program-derived missing skill associated with its source requirement."""

    requirement_id: NonEmptyStr
    skill: NonEmptyStr


class JobAnalysisDraft(FrozenDomainModel):
    """Model-authored analysis payload before guardrails and scoring."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    requirement_matches: tuple[RequirementMatchDraft, ...]
    strengths: tuple[GroundedClaim, ...] = ()
    resume_suggestions: tuple[ResumeSuggestion, ...] = ()
    interview_topics: tuple[InterviewTopic, ...] = ()
    next_action: NonEmptyStr

    @field_validator("requirement_matches")
    @classmethod
    def validate_matches(
        cls, matches: tuple[RequirementMatchDraft, ...]
    ) -> tuple[RequirementMatchDraft, ...]:
        """Require at most one match per requirement in a draft."""
        _require_unique(tuple(item.requirement_id for item in matches), "matched requirement_id")
        return matches

    @field_validator("strengths")
    @classmethod
    def validate_strengths(cls, strengths: tuple[GroundedClaim, ...]) -> tuple[GroundedClaim, ...]:
        """Require unique claim identities in a draft."""
        _require_unique(tuple(item.claim_id for item in strengths), "claim_id")
        return strengths


class ScoreGroupBreakdown(FrozenDomainModel):
    """Deterministic contribution of one populated importance group."""

    importance: RequirementImportance
    requirement_count: int = Field(ge=1)
    status_total: DecimalValue = Field(ge=0)
    status_mean: DecimalValue = Field(ge=0, le=1)
    base_weight: DecimalValue = Field(gt=0, le=1)
    normalized_weight: DecimalValue = Field(gt=0, le=1)
    weighted_contribution: DecimalValue = Field(ge=0, le=1)


class ScoreBreakdown(FrozenDomainModel):
    """Complete deterministic score calculation and rounded result."""

    groups: tuple[ScoreGroupBreakdown, ...] = Field(min_length=1)
    scored_requirement_ids: tuple[NonEmptyStr, ...] = ()
    excluded_requirement_ids: tuple[NonEmptyStr, ...] = ()
    raw_score: DecimalValue = Field(ge=0, le=100)
    score: int = Field(ge=0, le=100)

    @field_validator("groups")
    @classmethod
    def validate_groups(
        cls, groups: tuple[ScoreGroupBreakdown, ...]
    ) -> tuple[ScoreGroupBreakdown, ...]:
        """Require one entry per populated importance group."""
        values = tuple(group.importance.value for group in groups)
        _require_unique(values, "score importance group")
        return groups

    @model_validator(mode="after")
    def validate_requirement_scope(self) -> Self:
        """Keep scored and contextual requirement identities unique and disjoint."""
        _require_unique(self.scored_requirement_ids, "scored requirement_id")
        _require_unique(self.excluded_requirement_ids, "excluded requirement_id")
        overlap = set(self.scored_requirement_ids) & set(self.excluded_requirement_ids)
        if overlap:
            raise ValueError("scored and excluded requirement IDs must be disjoint")
        return self


class JobAnalysis(JobAnalysisDraft):
    """Validated, program-scored and persistence-ready JobIntel result."""

    analysis_id: NonEmptyStr
    run_id: NonEmptyStr
    score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    score_breakdown: ScoreBreakdown
    missing_skills: tuple[MissingSkill, ...] = ()
    prompt_version: NonEmptyStr
    parser_version: NonEmptyStr
    toolset_version: NonEmptyStr
    scoring_version: NonEmptyStr
    schema_version: NonEmptyStr
    provenance_version: NonEmptyStr
    provenance_digest: Sha256Hex
    created_at: UtcDateTime

    @model_validator(mode="after")
    def validate_score_consistency(self) -> Self:
        """Prevent disagreement between the top-level and breakdown scores."""
        if self.score != self.score_breakdown.score:
            raise ValueError("score must equal score_breakdown.score")
        return self
