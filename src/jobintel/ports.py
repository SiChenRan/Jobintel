"""Application-facing protocols for JobIntel adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jobintel.models import CandidateProfile, Company, JobAnalysis, JobPosting


@runtime_checkable
class JobRepository(Protocol):
    """Persistence operations used by future JobIntel services and tools."""

    def get_job(self, job_id: str, job_version: int | None = None) -> JobPosting:
        """Return a concrete job version, resolving latest when omitted."""
        ...

    def get_candidate_profile(
        self, candidate_id: str, profile_version: int | None = None
    ) -> CandidateProfile:
        """Return a concrete profile version, resolving latest when omitted."""
        ...

    def get_company(self, company_id: str) -> Company:
        """Return company context by ID."""
        ...

    def save_analysis(self, analysis: JobAnalysis) -> JobAnalysis:
        """Atomically save or idempotently return a complete analysis."""
        ...

    def save_staged_job_analysis(self, job: JobPosting, analysis: JobAnalysis) -> JobAnalysis:
        """Atomically save a raw-JD job revision and its finalized analysis."""
        ...

    def get_analysis(self, analysis_id: str) -> JobAnalysis:
        """Return a previously persisted analysis aggregate."""
        ...
