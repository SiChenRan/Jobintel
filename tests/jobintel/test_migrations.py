"""Tests for checksum-verified and atomic embedded migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from jobintel.errors import MigrationHistoryError, SchemaNotReadyError
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MIGRATIONS, Migration, MigrationRunner
from jobintel.persistence.repository import SQLiteJobRepository


@pytest.fixture
def empty_db() -> Iterator[JobIntelDatabase]:
    database = JobIntelDatabase.connect(":memory:")
    yield database
    database.close()


def test_initial_migration_creates_expected_schema_and_is_idempotent(
    empty_db: JobIntelDatabase,
) -> None:
    runner = MigrationRunner(empty_db)
    assert runner.status().pending_versions == (
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
    )

    first = runner.migrate()
    second = runner.migrate()

    assert first.is_current is True
    assert second == first
    tables = {
        row["name"]
        for row in empty_db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables >= {
        "schema_migrations",
        "companies",
        "jobs",
        "requirements",
        "candidate_profiles",
        "candidate_evidence",
        "application_analyses",
        "requirement_matches",
        "requirement_match_evidence",
        "discovery_runs",
        "discovered_jobs",
        "discovery_run_jobs",
        "discovery_source_attempts",
        "discovery_detail_attempts",
        "radar_checks",
        "radar_events",
        "outreach_drafts",
        "outreach_claims",
        "outreach_claim_requirements",
        "outreach_claim_evidence",
        "outreach_events",
        "email_notification_attempts",
        "candidate_email_preferences",
        "web_users",
        "web_sessions",
        "web_runtime_settings",
    }
    assert empty_db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    discovery_run_columns = {
        row["name"] for row in empty_db.connection.execute("PRAGMA table_info(discovery_runs)")
    }
    discovery_hit_columns = {
        row["name"] for row in empty_db.connection.execute("PRAGMA table_info(discovery_run_jobs)")
    }
    assert "profile_snapshot_json" in discovery_run_columns
    assert "rank_explanation_json" in discovery_hit_columns
    user_columns = {
        row["name"] for row in empty_db.connection.execute("PRAGMA table_info(web_users)")
    }
    assert {"role", "candidate_id", "is_active", "display_name", "email"} <= user_columns


def test_repository_rejects_unmigrated_database(empty_db: JobIntelDatabase) -> None:
    with pytest.raises(SchemaNotReadyError, match="pending migrations"):
        SQLiteJobRepository(empty_db)


def test_user_profile_migration_preserves_existing_accounts(
    empty_db: JobIntelDatabase,
) -> None:
    MigrationRunner(empty_db, migrations=MIGRATIONS[:12]).migrate()
    empty_db.connection.execute(
        """
        INSERT INTO web_users (
            user_id, username, username_normalized, password_hash, created_at,
            role, candidate_id, is_active
        ) VALUES ('legacy-admin', 'admin', 'admin', 'hash', '2026-09-01T00:00:00+00:00',
                  'admin', NULL, 1)
        """
    )
    empty_db.connection.commit()

    status = MigrationRunner(empty_db).migrate()
    row = empty_db.connection.execute(
        "SELECT username, display_name, email FROM web_users WHERE user_id = 'legacy-admin'"
    ).fetchone()

    assert status.is_current is True
    assert dict(row) == {"username": "admin", "display_name": "admin", "email": None}


def test_checksum_or_name_drift_is_rejected(empty_db: JobIntelDatabase) -> None:
    runner = MigrationRunner(empty_db)
    runner.migrate()
    empty_db.connection.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = 1", ("0" * 64,)
    )
    empty_db.connection.commit()

    with pytest.raises(MigrationHistoryError, match="name/checksum"):
        runner.status()


def test_database_newer_than_application_is_rejected(empty_db: JobIntelDatabase) -> None:
    runner = MigrationRunner(empty_db)
    runner.migrate()
    empty_db.connection.execute(
        """
        INSERT INTO schema_migrations (version, name, checksum, applied_at)
        VALUES (15, 'future', ?, '2026-09-04T00:00:00+00:00')
        """,
        ("f" * 64,),
    )
    empty_db.connection.commit()

    with pytest.raises(MigrationHistoryError, match="newer than this application"):
        runner.status()


def test_noncontiguous_migration_history_is_rejected(empty_db: JobIntelDatabase) -> None:
    empty_db.connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    empty_db.connection.execute(
        "INSERT INTO schema_migrations VALUES (2, 'future', ?, 'now')", ("f" * 64,)
    )
    empty_db.connection.commit()

    with pytest.raises(MigrationHistoryError, match="not contiguous"):
        MigrationRunner(empty_db).status()


def test_failed_migration_rolls_back_all_statements(empty_db: JobIntelDatabase) -> None:
    broken = Migration(
        version=1,
        name="broken",
        statements=(
            "CREATE TABLE transient_row (id INTEGER PRIMARY KEY)",
            "INSERT INTO table_that_does_not_exist VALUES (1)",
        ),
    )
    runner = MigrationRunner(empty_db, migrations=(broken,))

    with pytest.raises(sqlite3.OperationalError):
        runner.migrate()

    transient = empty_db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transient_row'"
    ).fetchone()
    applied = empty_db.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert transient is None
    assert applied == 0


def test_database_transaction_rejects_nesting_and_rolls_back(
    empty_db: JobIntelDatabase,
) -> None:
    empty_db.connection.execute("CREATE TABLE sample (value TEXT)")
    empty_db.connection.commit()

    with (
        pytest.raises(RuntimeError, match="nested"),
        empty_db.transaction(),
        empty_db.transaction(),
    ):
        pass
    assert empty_db.connection.in_transaction is False

    with pytest.raises(ValueError), empty_db.transaction() as connection:
        connection.execute("INSERT INTO sample VALUES ('not-committed')")
        raise ValueError("force rollback")
    assert empty_db.connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
