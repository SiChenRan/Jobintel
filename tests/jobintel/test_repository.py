"""Repository tests for version isolation and atomic analysis persistence."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from jobintel.errors import (
    EntityNotFoundError,
    IdempotencyConflictError,
    PersistenceValidationError,
)
from jobintel.models import (
    ANALYSIS_SCHEMA_VERSION,
    GroundedClaim,
    JobAnalysis,
    JobPosting,
    MatchStatus,
    RequirementMatchDraft,
    ResumeSuggestion,
)
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.ports import JobRepository
from jobintel.scoring import SCORING_VERSION, derive_recommendation, score_requirements

_NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
_PROVENANCE_DIGEST = "b" * 64


def _analysis(repository: SQLiteJobRepository) -> JobAnalysis:
    job = repository.get_job("J001", 1)
    evidence_by_requirement = (
        "ev-python-skill",
        "ev-python-api",
        "ev-python-api",
        "ev-atlas-platform",
    )
    matches = tuple(
        RequirementMatchDraft(
            requirement_id=requirement.requirement_id,
            status=MatchStatus.MATCHED,
            evidence_ids=(evidence_id,),
            confidence=0.9,
            reason="Fixture evidence supports this requirement.",
        )
        for requirement, evidence_id in zip(job.requirements, evidence_by_requirement, strict=True)
    )
    breakdown = score_requirements(job.requirements, matches)
    first = matches[0]
    return JobAnalysis(
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id="C001",
        profile_version=2,
        requirement_matches=matches,
        strengths=(
            GroundedClaim(
                claim_id="strength-python",
                text="Strong production Python background.",
                requirement_ids=(first.requirement_id,),
                evidence_ids=first.evidence_ids,
            ),
        ),
        next_action="Tailor the resume and apply.",
        analysis_id="analysis-001",
        run_id="run-001",
        score=breakdown.score,
        recommendation=derive_recommendation(breakdown.score),
        score_breakdown=breakdown,
        prompt_version="prompt-v1",
        parser_version="parser-v1",
        toolset_version="toolset-v1",
        scoring_version=SCORING_VERSION,
        schema_version=ANALYSIS_SCHEMA_VERSION,
        provenance_version="provenance-v1",
        provenance_digest=_PROVENANCE_DIGEST,
        created_at=_NOW,
    )


def _count(database: JobIntelDatabase, table: str) -> int:
    return int(database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _staged_job_analysis(
    repository: SQLiteJobRepository,
) -> tuple[JobPosting, JobAnalysis]:
    source = repository.get_job("J001", 1)
    requirements = tuple(
        requirement.model_copy(update={"requirement_id": f"raw-req-{index}"})
        for index, requirement in enumerate(source.requirements)
    )
    job = source.model_copy(
        update={
            "job_id": "job_raw_test",
            "requirements": requirements,
            "source_sha256": "d" * 64,
        }
    )
    original = _analysis(repository)
    matches = tuple(
        match.model_copy(update={"requirement_id": requirement.requirement_id})
        for match, requirement in zip(original.requirement_matches, requirements, strict=True)
    )
    analysis = original.model_copy(
        update={
            "analysis_id": "analysis-raw",
            "run_id": "run-raw-repository",
            "job_id": job.job_id,
            "requirement_matches": matches,
            "strengths": (),
            "resume_suggestions": (),
            "interview_topics": (),
            "missing_skills": (),
        }
    )
    return job, analysis


def test_repository_maps_latest_and_explicit_versions(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    original = jobintel_repo.get_job("J001", 1)
    version_two = original.model_copy(
        update={
            "job_version": 2,
            "title": "Senior Backend Platform Engineer",
            "source_sha256": "c" * 64,
            "requirements": (),
        }
    )
    with jobintel_db.transaction():
        jobintel_repo.insert_job(version_two)

    assert jobintel_repo.get_job("J001").job_version == 2
    assert jobintel_repo.get_job("J001", 1) == original
    assert jobintel_repo.get_candidate_profile("C001").profile_version == 2
    assert jobintel_repo.get_candidate_profile("C001", 1).profile_version == 1


def test_repository_not_found_errors_are_typed(jobintel_repo: SQLiteJobRepository) -> None:
    assert isinstance(jobintel_repo, JobRepository)
    with pytest.raises(EntityNotFoundError, match="job not found"):
        jobintel_repo.get_job("missing")
    with pytest.raises(EntityNotFoundError, match="candidate profile not found"):
        jobintel_repo.get_candidate_profile("missing")
    with pytest.raises(EntityNotFoundError, match="company not found"):
        jobintel_repo.get_company("missing")
    with pytest.raises(EntityNotFoundError, match="analysis not found"):
        jobintel_repo.get_analysis("missing")
    with pytest.raises(EntityNotFoundError, match="analysis run not found"):
        jobintel_repo.get_analysis_by_run_id("missing")


def test_evidence_identity_is_isolated_by_candidate_and_profile(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    version_one = jobintel_repo.get_candidate_profile("C001", 1)
    version_two = jobintel_repo.get_candidate_profile("C001", 2)
    other_candidate = jobintel_repo.get_candidate_profile("C002", 1)

    assert "ev-atlas-platform" not in {item.evidence_id for item in version_one.evidence}
    assert "ev-atlas-platform" in {item.evidence_id for item in version_two.evidence}
    assert "ev-apac-program" in {item.evidence_id for item in other_candidate.evidence}
    assert "ev-apac-program" not in {item.evidence_id for item in version_two.evidence}


def test_save_analysis_persists_and_rehydrates_complete_aggregate(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    analysis = _analysis(jobintel_repo)

    saved = jobintel_repo.save_analysis(analysis)

    assert saved == analysis
    assert jobintel_repo.get_analysis(analysis.analysis_id) == analysis
    assert jobintel_repo.get_analysis_by_run_id(analysis.run_id) == analysis
    assert _count(jobintel_db, "application_analyses") == 1
    assert _count(jobintel_db, "requirement_matches") == 4
    assert _count(jobintel_db, "requirement_match_evidence") == 4
    assert jobintel_db.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_list_analyses_can_scope_to_candidate(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    analysis = _analysis(jobintel_repo)
    jobintel_repo.save_analysis(analysis)

    assert jobintel_repo.list_analyses() == (analysis,)
    assert jobintel_repo.list_analyses(candidate_id=analysis.candidate_id) == (analysis,)
    assert jobintel_repo.list_analyses(candidate_id="missing") == ()


def test_save_analysis_is_idempotent_by_run_id(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    analysis = _analysis(jobintel_repo)
    first = jobintel_repo.save_analysis(analysis)
    second = jobintel_repo.save_analysis(analysis)

    assert second == first
    assert _count(jobintel_db, "application_analyses") == 1

    conflicting = analysis.model_copy(update={"next_action": "A different action."})
    with pytest.raises(IdempotencyConflictError, match="different payload"):
        jobintel_repo.save_analysis(conflicting)
    assert _count(jobintel_db, "application_analyses") == 1


def test_save_rejects_requirement_or_evidence_outside_versioned_scope(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    analysis = _analysis(jobintel_repo)
    missing_match = analysis.model_copy(
        update={"requirement_matches": analysis.requirement_matches[:-1]}
    )
    with pytest.raises(PersistenceValidationError, match="requirement scope mismatch"):
        jobintel_repo.save_analysis(missing_match)

    cross_candidate_match = analysis.requirement_matches[0].model_copy(
        update={"evidence_ids": ("ev-apac-program",)}
    )
    cross_candidate = analysis.model_copy(
        update={
            "requirement_matches": (cross_candidate_match, *analysis.requirement_matches[1:]),
            "strengths": (),
        }
    )
    with pytest.raises(PersistenceValidationError, match="candidate/profile scope"):
        jobintel_repo.save_analysis(cross_candidate)
    assert _count(jobintel_db, "application_analyses") == 0


def test_save_rechecks_strength_and_narrative_requirement_scope(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    analysis = _analysis(jobintel_repo)
    unknown_requirement = GroundedClaim(
        claim_id="unknown-requirement",
        text="Unsupported requirement.",
        requirement_ids=("req-unknown",),
        evidence_ids=("ev-python-api",),
    )
    with pytest.raises(PersistenceValidationError, match="unknown requirement"):
        jobintel_repo.save_analysis(
            analysis.model_copy(update={"strengths": (unknown_requirement,)})
        )

    wrong_evidence = GroundedClaim(
        claim_id="wrong-evidence",
        text="Evidence came from another requirement match.",
        requirement_ids=(analysis.requirement_matches[0].requirement_id,),
        evidence_ids=(analysis.requirement_matches[1].evidence_ids[0],),
    )
    with pytest.raises(PersistenceValidationError, match="outside its matches"):
        jobintel_repo.save_analysis(analysis.model_copy(update={"strengths": (wrong_evidence,)}))

    unknown_suggestion = ResumeSuggestion(
        requirement_id="req-unknown", text="Add unsupported material."
    )
    with pytest.raises(PersistenceValidationError, match="narrative references unknown"):
        jobintel_repo.save_analysis(
            analysis.model_copy(
                update={"strengths": (), "resume_suggestions": (unknown_suggestion,)}
            )
        )
    assert _count(jobintel_db, "application_analyses") == 0


def test_database_failure_rolls_back_analysis_matches_and_links(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    jobintel_db.connection.execute(
        """
        CREATE TRIGGER reject_analysis_evidence
        BEFORE INSERT ON requirement_match_evidence
        BEGIN
            SELECT RAISE(ABORT, 'injected failure');
        END
        """
    )
    jobintel_db.connection.commit()

    with pytest.raises(PersistenceValidationError, match="database integrity"):
        jobintel_repo.save_analysis(_analysis(jobintel_repo))

    assert _count(jobintel_db, "application_analyses") == 0
    assert _count(jobintel_db, "requirement_matches") == 0
    assert _count(jobintel_db, "requirement_match_evidence") == 0


def test_database_foreign_keys_reject_cross_profile_evidence(
    jobintel_db: JobIntelDatabase,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        jobintel_db.connection.execute(
            """
            INSERT INTO candidate_evidence (
                candidate_id, profile_version, evidence_id, evidence_type,
                title, content, skills_json, source_order
            ) VALUES ('UNKNOWN', 1, 'ev-x', 'skill', 'x', 'x', '[]', 0)
            """
        )
    jobintel_db.connection.rollback()


def test_staged_job_and_analysis_commit_or_rollback_as_one_aggregate(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    job, analysis = _staged_job_analysis(jobintel_repo)

    saved = jobintel_repo.save_staged_job_analysis(job, analysis)

    assert saved == analysis
    assert jobintel_repo.get_job("job_raw_test", 1) == job
    assert _count(jobintel_db, "application_analyses") == 1


def test_staged_job_rolls_back_when_analysis_insert_fails(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    job, analysis = _staged_job_analysis(jobintel_repo)
    jobintel_db.connection.execute(
        """
        CREATE TRIGGER reject_staged_analysis
        BEFORE INSERT ON application_analyses
        BEGIN
            SELECT RAISE(ABORT, 'injected staged failure');
        END
        """
    )
    jobintel_db.connection.commit()

    with pytest.raises(PersistenceValidationError, match="staged job analysis"):
        jobintel_repo.save_staged_job_analysis(job, analysis)

    with pytest.raises(EntityNotFoundError):
        jobintel_repo.get_job("job_raw_test", 1)
    assert _count(jobintel_db, "application_analyses") == 0


def test_staged_job_requires_matching_analysis_identity(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, analysis = _staged_job_analysis(jobintel_repo)

    with pytest.raises(PersistenceValidationError, match="identity does not match"):
        jobintel_repo.save_staged_job_analysis(
            job, analysis.model_copy(update={"job_id": "another-job"})
        )


def test_staged_job_save_is_idempotent_and_rejects_changed_job_content(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, analysis = _staged_job_analysis(jobintel_repo)
    first = jobintel_repo.save_staged_job_analysis(job, analysis)

    assert jobintel_repo.save_staged_job_analysis(job, analysis) == first

    next_analysis = analysis.model_copy(
        update={"analysis_id": "analysis-raw-2", "run_id": "run-raw-repository-2"}
    )
    assert jobintel_repo.save_staged_job_analysis(job, next_analysis) == next_analysis

    changed_job = job.model_copy(update={"title": "Changed parser output"})
    third_analysis = analysis.model_copy(
        update={"analysis_id": "analysis-raw-3", "run_id": "run-raw-repository-3"}
    )
    with pytest.raises(IdempotencyConflictError, match="different content"):
        jobintel_repo.save_staged_job_analysis(changed_job, third_analysis)
