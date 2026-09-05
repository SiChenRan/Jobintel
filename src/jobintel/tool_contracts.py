"""Single source of truth for JobIntel tool names and Pydantic contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, Field, JsonValue, model_validator

from jobintel.models import (
    CandidateProfile,
    Company,
    FrozenDomainModel,
    JobAnalysis,
    JobAnalysisDraft,
    JobPosting,
    JobRequirement,
    NonEmptyStr,
    SearchCandidateEvidenceOutput,
    Sha256Hex,
    UtcDateTime,
)
from jobintel.providers.base import ToolSpec

TOOLSET_VERSION = "jobintel-toolset-v1"
STORED_REQUIREMENTS_PARSER_VERSION = "stored-requirements-v1"


class ToolEffect(StrEnum):
    """Side-effect category used by dispatch and MCP annotations."""

    READ = "read"
    COMPUTE = "compute"
    WRITE = "write"


class ToolErrorCode(StrEnum):
    """Stable error codes shared by in-process and FastMCP adapters."""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    DUPLICATE_TOOL_CALL = "DUPLICATE_TOOL_CALL"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    NOT_FOUND = "NOT_FOUND"
    INVALID_SCOPE = "INVALID_SCOPE"
    PARSER_NOT_AVAILABLE = "PARSER_NOT_AVAILABLE"
    PARSER_REPAIR_LIMIT = "PARSER_REPAIR_LIMIT"
    INVALID_TERMINAL_TURN = "INVALID_TERMINAL_TURN"
    GUARDRAIL_REJECTED = "GUARDRAIL_REJECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PERSISTENCE_REJECTED = "PERSISTENCE_REJECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolErrorEnvelope(FrozenDomainModel):
    """Transport-neutral structured tool failure."""

    code: ToolErrorCode
    message: NonEmptyStr
    retryable: bool
    field_path: NonEmptyStr | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class GetJobRequest(FrozenDomainModel):
    """Request a stored job, optionally at an explicit version."""

    job_id: NonEmptyStr
    job_version: int | None = Field(default=None, ge=1)


class ParseJobRequirementsRequest(FrozenDomainModel):
    """Request requirements from exactly one stored job or raw JD input."""

    job_id: NonEmptyStr | None = None
    job_version: int | None = Field(default=None, ge=1)
    jd_text: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """Require exactly one source and scope versions to stored jobs."""
        if (self.job_id is None) == (self.jd_text is None):
            raise ValueError("exactly one of job_id or jd_text is required")
        if self.jd_text is not None and self.job_version is not None:
            raise ValueError("job_version is only valid with job_id")
        return self


class ParsedJobRequirements(FrozenDomainModel):
    """Requirements resolved by a versioned parser implementation."""

    job_id: NonEmptyStr | None = None
    job_version: int | None = Field(default=None, ge=1)
    requirements: tuple[JobRequirement, ...]
    source_sha256: Sha256Hex
    parser_version: NonEmptyStr


class GetCandidateProfileRequest(FrozenDomainModel):
    """Request a profile summary at an optional immutable version."""

    candidate_id: NonEmptyStr
    profile_version: int | None = Field(default=None, ge=1)


class CandidateProfileSummary(FrozenDomainModel):
    """Non-citable profile overview; evidence content stays behind search."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    summary: NonEmptyStr | None = None
    skills: tuple[NonEmptyStr, ...]
    evidence_count: int = Field(ge=0)
    source_sha256: Sha256Hex
    created_at: UtcDateTime

    @classmethod
    def from_profile(cls, profile: CandidateProfile) -> CandidateProfileSummary:
        """Project a full profile to a deterministic content-free summary."""
        skills_by_key: dict[str, str] = {}
        for evidence in profile.evidence:
            for skill in evidence.skills:
                skills_by_key.setdefault(skill.casefold(), skill)
        return cls(
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
            summary=profile.summary,
            skills=tuple(skills_by_key[key] for key in sorted(skills_by_key)),
            evidence_count=len(profile.evidence),
            source_sha256=profile.source_sha256,
            created_at=profile.created_at,
        )


class SearchCandidateEvidenceRequest(FrozenDomainModel):
    """Fully scoped candidate evidence search request."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    requirement_id: NonEmptyStr
    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    query: NonEmptyStr
    top_k: int = Field(default=5, ge=1, le=20)


class GetCompanyRequest(FrozenDomainModel):
    """Request company context by stable ID."""

    company_id: NonEmptyStr


class SaveAnalysisResult(FrozenDomainModel):
    """Terminal tool result containing the finalized persisted aggregate."""

    analysis: JobAnalysis


@dataclass(frozen=True)
class ToolContract:
    """Transport-neutral definition of one JobIntel tool."""

    name: str
    description: str
    effect: ToolEffect
    request_model: type[BaseModel]
    response_model: type[BaseModel]

    @property
    def is_terminal(self) -> bool:
        """Return whether this tool is the sole terminal write operation."""
        return self.effect is ToolEffect.WRITE

    def input_schema(self) -> dict[str, Any]:
        """Return the canonical provider/MCP input schema."""
        schema = self.request_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def output_schema(self) -> dict[str, Any]:
        """Return the canonical MCP output schema."""
        schema = self.response_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def provider_spec(self) -> ToolSpec:
        """Project this contract to the provider-neutral tool protocol."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema(),
        )


TOOL_CONTRACTS = (
    ToolContract(
        name="get_job",
        description="Fetch one immutable stored job version; omit version to resolve latest.",
        effect=ToolEffect.READ,
        request_model=GetJobRequest,
        response_model=JobPosting,
    ),
    ToolContract(
        name="parse_job_requirements",
        description="Resolve structured requirements from a stored job or raw JD text.",
        effect=ToolEffect.COMPUTE,
        request_model=ParseJobRequirementsRequest,
        response_model=ParsedJobRequirements,
    ),
    ToolContract(
        name="get_candidate_profile",
        description=(
            "Fetch a candidate profile summary and skill index. This does not return "
            "citable evidence; use search_candidate_evidence for citations."
        ),
        effect=ToolEffect.READ,
        request_model=GetCandidateProfileRequest,
        response_model=CandidateProfileSummary,
    ),
    ToolContract(
        name="search_candidate_evidence",
        description=(
            "Search evidence within one explicit Job Requirement and Candidate Profile scope."
        ),
        effect=ToolEffect.READ,
        request_model=SearchCandidateEvidenceRequest,
        response_model=SearchCandidateEvidenceOutput,
    ),
    ToolContract(
        name="get_company",
        description="Fetch optional company context by company ID.",
        effect=ToolEffect.READ,
        request_model=GetCompanyRequest,
        response_model=Company,
    ),
    ToolContract(
        name="save_application_analysis",
        description=(
            "Submit the final JobAnalysisDraft for guardrails, deterministic scoring, "
            "recommendation derivation, and atomic persistence."
        ),
        effect=ToolEffect.WRITE,
        request_model=JobAnalysisDraft,
        response_model=SaveAnalysisResult,
    ),
)

TOOL_CONTRACT_BY_NAME = MappingProxyType({contract.name: contract for contract in TOOL_CONTRACTS})
