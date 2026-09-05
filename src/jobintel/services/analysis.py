"""Finalization trust boundary for JobIntel analysis drafts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from jobintel.guardrail import GuardrailViolation, validate_analysis_draft
from jobintel.models import (
    ANALYSIS_SCHEMA_VERSION,
    JobAnalysis,
    JobAnalysisDraft,
    JobPosting,
    MatchStatus,
    MissingSkill,
)
from jobintel.ports import JobRepository
from jobintel.provenance import PROVENANCE_VERSION, ProvenanceLedger
from jobintel.scoring import (
    SCORING_VERSION,
    derive_recommendation,
    is_scoreable_requirement,
    score_requirements,
)
from jobintel.tool_contracts import STORED_REQUIREMENTS_PARSER_VERSION, TOOLSET_VERSION

DEFAULT_PROMPT_VERSION = "jobintel-agent-v1"


@dataclass(frozen=True)
class AnalysisVersions:
    """Version metadata stamped onto every finalized analysis."""

    prompt: str = DEFAULT_PROMPT_VERSION
    parser: str = STORED_REQUIREMENTS_PARSER_VERSION
    toolset: str = TOOLSET_VERSION
    scoring: str = SCORING_VERSION
    schema: str = ANALYSIS_SCHEMA_VERSION
    provenance: str = PROVENANCE_VERSION


DEFAULT_ANALYSIS_VERSIONS = AnalysisVersions()


class AnalysisFinalizationError(ValueError):
    """Raised when a draft fails deterministic provenance guardrails."""

    def __init__(self, violations: tuple[GuardrailViolation, ...]) -> None:
        """Retain structured violations without embedding private source text."""
        self.violations = violations
        super().__init__(f"analysis draft failed {len(violations)} guardrail checks")


def stable_analysis_id(run_id: str) -> str:
    """Derive a program-controlled analysis ID from the run idempotency key."""
    normalized = run_id.strip()
    if not normalized:
        raise ValueError("run_id must not be empty")
    digest = hashlib.sha256(f"jobintel-analysis-v1\n{normalized}".encode()).hexdigest()
    return f"analysis_{digest[:20]}"


class AnalysisService:
    """Guard, score, enrich, and atomically persist a model-authored draft."""

    def __init__(
        self,
        repository: JobRepository,
        ledger: ProvenanceLedger,
        *,
        versions: AnalysisVersions = DEFAULT_ANALYSIS_VERSIONS,
        clock: Callable[[], datetime] | None = None,
        staged_job: JobPosting | None = None,
        persist: bool = True,
    ) -> None:
        """Bind the finalizer to one repository and one run ledger."""
        self._repository = repository
        self._ledger = ledger
        self._versions = versions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._staged_job = staged_job
        self._persist = persist

    def finalize_and_save(self, draft: JobAnalysisDraft) -> JobAnalysis:
        """Apply every program-controlled finalization stage in order."""
        job = self._resolve_job(draft)
        profile = self._repository.get_candidate_profile(draft.candidate_id, draft.profile_version)
        guardrail = validate_analysis_draft(
            draft=draft,
            job=job,
            profile=profile,
            ledger=self._ledger,
        )
        if not guardrail.is_valid:
            raise AnalysisFinalizationError(guardrail.violations)

        breakdown = score_requirements(job.requirements, draft.requirement_matches)
        missing_skills = tuple(
            MissingSkill(
                requirement_id=requirement.requirement_id,
                skill=requirement.normalized_skill or requirement.text,
            )
            for requirement in job.requirements
            if is_scoreable_requirement(requirement)
            if next(
                match
                for match in draft.requirement_matches
                if match.requirement_id == requirement.requirement_id
            ).status
            is MatchStatus.MISSING
        )
        analysis = JobAnalysis(
            **draft.model_dump(),
            analysis_id=stable_analysis_id(self._ledger.run_id),
            run_id=self._ledger.run_id,
            score=breakdown.score,
            recommendation=derive_recommendation(breakdown.score),
            score_breakdown=breakdown,
            missing_skills=missing_skills,
            prompt_version=self._versions.prompt,
            parser_version=self._versions.parser,
            toolset_version=self._versions.toolset,
            scoring_version=self._versions.scoring,
            schema_version=self._versions.schema,
            provenance_version=self._versions.provenance,
            provenance_digest=self._ledger.snapshot().digest,
            created_at=self._clock(),
        )
        if not self._persist:
            return analysis
        if self._staged_job is not None:
            return self._repository.save_staged_job_analysis(self._staged_job, analysis)
        return self._repository.save_analysis(analysis)

    def _resolve_job(self, draft: JobAnalysisDraft) -> JobPosting:
        """Resolve the exact persisted or staged job revision for this draft."""
        if self._staged_job is None:
            return self._repository.get_job(draft.job_id, draft.job_version)
        return self._staged_job
