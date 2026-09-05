"""Tests for validated, atomic, versioned JobIntel fixture loading."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from jobintel.errors import SeedDataError
from jobintel.models import Company, EvidenceType
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database

_SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "jobintel_seed"


def test_seed_is_idempotent_and_covers_required_demo_shapes(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    first = seed_database(jobintel_db)
    second = seed_database(jobintel_db)

    assert (
        first
        == second
        == {
            "companies": 3,
            "jobs": 3,
            "requirements": 12,
            "candidate_profiles": 3,
            "candidate_evidence": 10,
        }
    )
    assert jobintel_repo.get_job("J001").title == "Backend Platform Engineer"
    assert jobintel_repo.get_job("J002").title == "Machine Learning Engineer"
    assert jobintel_repo.get_job("J003").title == "Technical Program Manager"
    assert jobintel_repo.get_candidate_profile("C001").profile_version == 2
    assert jobintel_repo.get_candidate_profile("C001", 1).profile_version == 1
    evidence_types = {
        evidence.evidence_type
        for candidate_id, version in (("C001", 1), ("C001", 2), ("C002", 1))
        for evidence in jobintel_repo.get_candidate_profile(candidate_id, version).evidence
    }
    assert evidence_types >= {
        EvidenceType.EXPERIENCE,
        EvidenceType.PROJECT,
        EvidenceType.SKILL,
        EvidenceType.EDUCATION,
        EvidenceType.CERTIFICATE,
    }


def test_invalid_fixture_is_rejected_before_existing_rows_are_reset(
    jobintel_db: JobIntelDatabase,
    jobintel_repo: SQLiteJobRepository,
    tmp_path: Path,
) -> None:
    custom = Company(company_id="keep-me", name="Existing company")
    with jobintel_db.transaction():
        jobintel_repo.insert_company(custom)

    broken_dir = tmp_path / "broken-seed"
    shutil.copytree(_SEED_DIR, broken_dir)
    evidence_path = broken_dir / "candidate_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence[0]["candidate_id"] = "UNKNOWN"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(SeedDataError, match="unknown candidate profile"):
        seed_database(jobintel_db, broken_dir)

    assert jobintel_repo.get_company("keep-me") == custom


def test_seed_database_behaves_the_same_for_file_and_memory_databases(tmp_path: Path) -> None:
    snapshots = []
    file_database_path = tmp_path / "jobintel.db"
    for database_path in (":memory:", file_database_path):
        with JobIntelDatabase.connect(database_path) as database:
            stats = seed_database(database)
            repository = SQLiteJobRepository(database)
            snapshots.append(
                (
                    stats,
                    repository.get_job("J001"),
                    repository.get_candidate_profile("C001", 2),
                )
            )
    assert snapshots[0] == snapshots[1]

    with JobIntelDatabase.connect(file_database_path) as reopened:
        repository = SQLiteJobRepository(reopened)
        assert repository.get_job("J001") == snapshots[0][1]
        assert repository.get_candidate_profile("C001", 2) == snapshots[0][2]


def test_seed_database_rolls_back_reset_and_partial_inserts_on_database_error(
    jobintel_db: JobIntelDatabase, jobintel_repo: SQLiteJobRepository
) -> None:
    custom = Company(company_id="keep-after-rollback", name="Existing company")
    with jobintel_db.transaction():
        jobintel_repo.insert_company(custom)
    before_jobs = jobintel_db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    jobintel_db.connection.execute(
        """
        CREATE TRIGGER reject_second_seed_job
        BEFORE INSERT ON jobs WHEN NEW.job_id = 'J002'
        BEGIN
            SELECT RAISE(ABORT, 'injected seed failure');
        END
        """
    )
    jobintel_db.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected seed failure"):
        seed_database(jobintel_db)

    assert jobintel_repo.get_company("keep-after-rollback") == custom
    assert jobintel_db.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
