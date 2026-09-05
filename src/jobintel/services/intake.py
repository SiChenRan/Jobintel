"""Resolve stored and raw-JD analysis inputs to immutable run scope."""

from __future__ import annotations

import uuid
from typing import Self

from pydantic import Field, model_validator

from jobintel.models import FrozenDomainModel, JobPosting, NonEmptyStr
from jobintel.ports import JobRepository
from jobintel.services.jd_parser import JDParserService, ParserTelemetry
from jobintel.tool_contracts import STORED_REQUIREMENTS_PARSER_VERSION


def _new_run_id() -> str:
    """Generate a caller-independent idempotency key for one analysis request."""
    return f"run_{uuid.uuid4().hex}"


class AnalysisRequest(FrozenDomainModel):
    """One requested Job Version by Candidate Profile Version analysis run."""

    run_id: NonEmptyStr = Field(default_factory=_new_run_id)
    candidate_id: NonEmptyStr
    profile_version: int | None = Field(default=None, ge=1)
    job_id: NonEmptyStr | None = None
    job_version: int | None = Field(default=None, ge=1)
    jd_text: NonEmptyStr | None = None
    jd_source_url: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """Require exactly one job source and scope its optional fields."""
        if (self.job_id is None) == (self.jd_text is None):
            raise ValueError("exactly one of job_id or jd_text is required")
        if self.job_id is None and self.job_version is not None:
            raise ValueError("job_version is only valid with job_id")
        if self.jd_text is None and self.jd_source_url is not None:
            raise ValueError("jd_source_url is only valid with jd_text")
        return self


class ResolvedAnalysisIntake(FrozenDomainModel):
    """Concrete immutable scope prepared before the agent tool loop."""

    job: JobPosting
    candidate_id: NonEmptyStr
    profile_version: int
    is_raw_job: bool
    parser_version: NonEmptyStr
    parser_telemetry: ParserTelemetry | None = None


class AnalysisIntakeService:
    """Pin latest versions and stage raw jobs without writing persistence."""

    def __init__(self, repository: JobRepository, parser: JDParserService | None = None) -> None:
        """Bind persistence and an optional raw-JD parser."""
        self._repository = repository
        self._parser = parser

    async def resolve(self, request: AnalysisRequest) -> ResolvedAnalysisIntake:
        """Resolve all optional versions and parse raw input before agent execution."""
        profile = self._repository.get_candidate_profile(
            request.candidate_id, request.profile_version
        )
        if request.job_id is not None:
            job = self._repository.get_job(request.job_id, request.job_version)
            return ResolvedAnalysisIntake(
                job=job,
                candidate_id=profile.candidate_id,
                profile_version=profile.profile_version,
                is_raw_job=False,
                parser_version=STORED_REQUIREMENTS_PARSER_VERSION,
            )
        if self._parser is None:
            raise RuntimeError("raw JD intake requires a JDParserService")
        if request.jd_text is None:  # validated by AnalysisRequest
            raise RuntimeError("raw JD request has no text")
        parsed = await self._parser.parse(request.jd_text, source_url=request.jd_source_url)
        return ResolvedAnalysisIntake(
            job=parsed.job,
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
            is_raw_job=True,
            parser_version=parsed.telemetry.prompt_version,
            parser_telemetry=parsed.telemetry,
        )
