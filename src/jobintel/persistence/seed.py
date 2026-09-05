"""Validate and atomically load versioned JobIntel JSON fixtures."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from importlib import resources
from pathlib import Path

from pydantic import Field, field_validator

from jobintel.errors import SeedDataError
from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    Company,
    FrozenDomainModel,
    JobPosting,
    JobRequirement,
    NonEmptyStr,
    RequirementCategory,
    RequirementImportance,
    UtcDateTime,
    canonicalize_requirement_text,
    stable_requirement_id,
)
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MigrationRunner
from jobintel.persistence.repository import SQLiteJobRepository


class _SeedRequirement(FrozenDomainModel):
    """Fixture requirement before program assignment of its stable ID."""

    text: NonEmptyStr
    category: RequirementCategory
    importance: RequirementImportance
    normalized_skill: NonEmptyStr | None = None
    source_order: int = Field(ge=0)


class _SeedJob(FrozenDomainModel):
    """Fixture job before source hashing and requirement ID assignment."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    company_id: NonEmptyStr | None = None
    company_name: NonEmptyStr
    title: NonEmptyStr
    location: NonEmptyStr | None = None
    employment_type: NonEmptyStr | None = None
    description: NonEmptyStr
    requirements: tuple[_SeedRequirement, ...]
    source_url: NonEmptyStr | None = None
    created_at: UtcDateTime

    @field_validator("requirements")
    @classmethod
    def validate_source_order(
        cls, requirements: tuple[_SeedRequirement, ...]
    ) -> tuple[_SeedRequirement, ...]:
        """Require deterministic and unique source ordering."""
        orders = [requirement.source_order for requirement in requirements]
        if len(orders) != len(set(orders)):
            raise ValueError("fixture requirement source_order values must be unique")
        return requirements


class _SeedProfile(FrozenDomainModel):
    """Fixture profile metadata before evidence attachment and source hashing."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    summary: NonEmptyStr | None = None
    created_at: UtcDateTime


class _SeedEvidence(CandidateEvidence):
    """Fixture evidence carrying its owning profile identity."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)


def _seed_dir() -> Path:
    """Locate repository fixtures or their installed package-data copy."""
    repository_path = Path(__file__).resolve().parents[3] / "data" / "jobintel_seed"
    if repository_path.exists():
        return repository_path
    return Path(str(resources.files("jobintel") / "data" / "jobintel_seed"))


def _load(name: str, directory: Path) -> list[dict[str, object]]:
    """Load one JSON array from the fixture directory."""
    data: object = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SeedDataError(f"fixture {name}.json must contain an array of objects")
    return data


def _source_sha256(value: object) -> str:
    """Hash canonical fixture content for immutable source identity."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _materialize_job(seed: _SeedJob) -> JobPosting:
    """Generate stable requirement IDs and construct a validated job version."""
    occurrence_by_key: defaultdict[tuple[str, RequirementCategory], int] = defaultdict(int)
    requirements = []
    for item in sorted(seed.requirements, key=lambda requirement: requirement.source_order):
        key = (canonicalize_requirement_text(item.text), item.category)
        occurrence_by_key[key] += 1
        requirements.append(
            JobRequirement(
                requirement_id=stable_requirement_id(
                    job_id=seed.job_id,
                    job_version=seed.job_version,
                    text=item.text,
                    category=item.category,
                    duplicate_ordinal=occurrence_by_key[key],
                ),
                **item.model_dump(),
            )
        )
    payload = seed.model_dump(exclude={"requirements"}, mode="json")
    return JobPosting(
        **payload,
        requirements=tuple(requirements),
        source_sha256=_source_sha256(seed.model_dump(mode="json")),
    )


def _parse_fixtures(
    directory: Path,
) -> tuple[list[Company], list[JobPosting], list[CandidateProfile]]:
    """Parse all files and validate cross-file references before any reset."""
    companies = [Company.model_validate(item) for item in _load("companies", directory)]
    seed_jobs = [_SeedJob.model_validate(item) for item in _load("jobs", directory)]
    seed_profiles = [
        _SeedProfile.model_validate(item) for item in _load("candidate_profiles", directory)
    ]
    seed_evidence = [
        _SeedEvidence.model_validate(item) for item in _load("candidate_evidence", directory)
    ]

    company_ids = [company.company_id for company in companies]
    if len(company_ids) != len(set(company_ids)):
        raise SeedDataError("company fixture contains duplicate company_id values")
    company_id_set = set(company_ids)

    jobs = [_materialize_job(seed) for seed in seed_jobs]
    job_keys = [(job.job_id, job.job_version) for job in jobs]
    if len(job_keys) != len(set(job_keys)):
        raise SeedDataError("job fixture contains duplicate job version identities")
    for job in jobs:
        if job.company_id is not None and job.company_id not in company_id_set:
            raise SeedDataError(f"job references unknown company_id: {job.company_id}")

    profile_keys = [(profile.candidate_id, profile.profile_version) for profile in seed_profiles]
    if len(profile_keys) != len(set(profile_keys)):
        raise SeedDataError("profile fixture contains duplicate profile version identities")
    profile_key_set = set(profile_keys)
    evidence_by_profile: defaultdict[tuple[str, int], list[CandidateEvidence]] = defaultdict(list)
    for item in seed_evidence:
        key = (item.candidate_id, item.profile_version)
        if key not in profile_key_set:
            raise SeedDataError(
                "evidence references unknown candidate profile: "
                f"{item.candidate_id}@{item.profile_version}"
            )
        evidence_by_profile[key].append(
            CandidateEvidence.model_validate(
                item.model_dump(exclude={"candidate_id", "profile_version"})
            )
        )

    profiles = []
    for seed_profile in seed_profiles:
        key = (seed_profile.candidate_id, seed_profile.profile_version)
        evidence = tuple(sorted(evidence_by_profile[key], key=lambda item: item.source_order))
        source_payload = {
            "profile": seed_profile.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        }
        profiles.append(
            CandidateProfile(
                **seed_profile.model_dump(mode="json"),
                evidence=evidence,
                source_sha256=_source_sha256(source_payload),
            )
        )
    return companies, jobs, profiles


def seed_database(database: JobIntelDatabase, seed_dir: Path | None = None) -> dict[str, int]:
    """Migrate, fully validate, then atomically reset and load JobIntel data."""
    MigrationRunner(database).migrate()
    companies, jobs, profiles = _parse_fixtures(seed_dir or _seed_dir())
    repository = SQLiteJobRepository(database)

    with database.transaction():
        repository.reset_all()
        for company in companies:
            repository.insert_company(company)
        for job in jobs:
            repository.insert_job(job)
        for profile in profiles:
            repository.insert_candidate_profile(profile)

    return {
        "companies": len(companies),
        "jobs": len(jobs),
        "requirements": sum(len(job.requirements) for job in jobs),
        "candidate_profiles": len(profiles),
        "candidate_evidence": sum(len(profile.evidence) for profile in profiles),
    }
