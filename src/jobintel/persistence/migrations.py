"""Checksum-verified embedded migrations for the JobIntel SQLite schema."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from jobintel.errors import MigrationHistoryError, SchemaNotReadyError
from jobintel.persistence.db import JobIntelDatabase

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

_INITIAL_SCHEMA = (
    """
    CREATE TABLE companies (
        company_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        industry TEXT,
        headquarters TEXT,
        website TEXT,
        description TEXT
    )
    """,
    """
    CREATE TABLE jobs (
        job_id TEXT NOT NULL,
        job_version INTEGER NOT NULL CHECK (job_version >= 1),
        company_id TEXT REFERENCES companies(company_id),
        company_name TEXT NOT NULL,
        title TEXT NOT NULL,
        location TEXT,
        employment_type TEXT,
        description TEXT NOT NULL,
        source_url TEXT,
        source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, job_version)
    )
    """,
    """
    CREATE TABLE requirements (
        job_id TEXT NOT NULL,
        job_version INTEGER NOT NULL,
        requirement_id TEXT NOT NULL,
        text TEXT NOT NULL,
        category TEXT NOT NULL,
        importance TEXT NOT NULL,
        normalized_skill TEXT,
        source_order INTEGER NOT NULL CHECK (source_order >= 0),
        PRIMARY KEY (job_id, job_version, requirement_id),
        UNIQUE (job_id, job_version, source_order),
        FOREIGN KEY (job_id, job_version)
            REFERENCES jobs(job_id, job_version) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE candidate_profiles (
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
        summary TEXT,
        source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
        created_at TEXT NOT NULL,
        PRIMARY KEY (candidate_id, profile_version)
    )
    """,
    """
    CREATE TABLE candidate_evidence (
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        evidence_id TEXT NOT NULL,
        evidence_type TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        skills_json TEXT NOT NULL,
        source_order INTEGER NOT NULL CHECK (source_order >= 0),
        PRIMARY KEY (candidate_id, profile_version, evidence_id),
        UNIQUE (candidate_id, profile_version, source_order),
        FOREIGN KEY (candidate_id, profile_version)
            REFERENCES candidate_profiles(candidate_id, profile_version) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE application_analyses (
        analysis_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL,
        job_version INTEGER NOT NULL,
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
        recommendation TEXT NOT NULL,
        score_breakdown_json TEXT NOT NULL,
        strengths_json TEXT NOT NULL,
        resume_suggestions_json TEXT NOT NULL,
        interview_topics_json TEXT NOT NULL,
        missing_skills_json TEXT NOT NULL,
        next_action TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        toolset_version TEXT NOT NULL,
        scoring_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        provenance_version TEXT NOT NULL,
        provenance_digest TEXT NOT NULL CHECK (length(provenance_digest) = 64),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        UNIQUE (analysis_id, job_id, job_version, candidate_id, profile_version),
        FOREIGN KEY (job_id, job_version) REFERENCES jobs(job_id, job_version),
        FOREIGN KEY (candidate_id, profile_version)
            REFERENCES candidate_profiles(candidate_id, profile_version)
    )
    """,
    """
    CREATE TABLE requirement_matches (
        analysis_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        job_version INTEGER NOT NULL,
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        reason TEXT NOT NULL,
        PRIMARY KEY (analysis_id, requirement_id),
        UNIQUE (analysis_id, requirement_id, candidate_id, profile_version),
        FOREIGN KEY (analysis_id, job_id, job_version, candidate_id, profile_version)
            REFERENCES application_analyses(
                analysis_id, job_id, job_version, candidate_id, profile_version
            ) ON DELETE CASCADE,
        FOREIGN KEY (job_id, job_version, requirement_id)
            REFERENCES requirements(job_id, job_version, requirement_id)
    )
    """,
    """
    CREATE TABLE requirement_match_evidence (
        analysis_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        evidence_id TEXT NOT NULL,
        citation_order INTEGER NOT NULL CHECK (citation_order >= 0),
        PRIMARY KEY (analysis_id, requirement_id, evidence_id),
        UNIQUE (analysis_id, requirement_id, citation_order),
        FOREIGN KEY (analysis_id, requirement_id, candidate_id, profile_version)
            REFERENCES requirement_matches(
                analysis_id, requirement_id, candidate_id, profile_version
            ) ON DELETE CASCADE,
        FOREIGN KEY (candidate_id, profile_version, evidence_id)
            REFERENCES candidate_evidence(candidate_id, profile_version, evidence_id)
    )
    """,
    "CREATE INDEX idx_jobs_company ON jobs(company_id)",
    "CREATE INDEX idx_requirements_job ON requirements(job_id, job_version, source_order)",
    """
    CREATE INDEX idx_evidence_profile
        ON candidate_evidence(candidate_id, profile_version, source_order)
    """,
    "CREATE INDEX idx_analyses_job ON application_analyses(job_id, job_version)",
    """
    CREATE INDEX idx_analyses_candidate
        ON application_analyses(candidate_id, profile_version)
    """,
)

_DISCOVERY_SCHEMA = (
    """
    CREATE TABLE discovery_runs (
        run_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        preference_json TEXT NOT NULL,
        total_discovered INTEGER NOT NULL CHECK (total_discovered >= 0),
        duplicates_removed INTEGER NOT NULL CHECK (duplicates_removed >= 0),
        filtered_out INTEGER NOT NULL CHECK (filtered_out >= 0),
        schema_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (candidate_id, profile_version)
            REFERENCES candidate_profiles(candidate_id, profile_version) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE discovered_jobs (
        discovery_job_id TEXT PRIMARY KEY,
        canonical_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        company_name TEXT NOT NULL,
        location TEXT NOT NULL,
        salary_text TEXT NOT NULL,
        salary_min_k INTEGER CHECK (salary_min_k IS NULL OR salary_min_k >= 0),
        salary_max_k INTEGER CHECK (salary_max_k IS NULL OR salary_max_k >= 0),
        description TEXT NOT NULL,
        experience TEXT NOT NULL,
        education TEXT NOT NULL,
        published_text TEXT NOT NULL,
        source_links_json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE discovery_run_jobs (
        run_id TEXT NOT NULL,
        discovery_job_id TEXT NOT NULL,
        rank_order INTEGER NOT NULL CHECK (rank_order >= 0),
        rank_score INTEGER NOT NULL CHECK (rank_score BETWEEN 0 AND 100),
        matched_terms_json TEXT NOT NULL,
        PRIMARY KEY (run_id, discovery_job_id),
        UNIQUE (run_id, rank_order),
        FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (discovery_job_id)
            REFERENCES discovered_jobs(discovery_job_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE discovery_source_attempts (
        run_id TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        discovered_count INTEGER NOT NULL CHECK (discovered_count >= 0),
        elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
        message TEXT,
        PRIMARY KEY (run_id, source),
        FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_discovery_runs_candidate ON discovery_runs(candidate_id, profile_version)",
    "CREATE INDEX idx_discovery_jobs_last_seen ON discovered_jobs(last_seen_at)",
)

_DISCOVERY_DETAIL_SCHEMA = (
    "ALTER TABLE discovered_jobs ADD COLUMN skills_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE discovered_jobs ADD COLUMN company_description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discovered_jobs ADD COLUMN recruiter_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discovered_jobs ADD COLUMN recruiter_title TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discovered_jobs ADD COLUMN recruiter_active_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discovered_jobs ADD COLUMN detail_fetched_at TEXT",
    "ALTER TABLE discovered_jobs ADD COLUMN detail_content_sha256 TEXT",
    """
    CREATE TABLE discovery_detail_attempts (
        run_id TEXT NOT NULL,
        discovery_job_id TEXT NOT NULL,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        status TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
        message TEXT,
        PRIMARY KEY (run_id, discovery_job_id, source, external_id),
        FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (discovery_job_id)
            REFERENCES discovered_jobs(discovery_job_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_discovery_details_fetched
        ON discovered_jobs(detail_fetched_at)
    """,
)

_DISCOVERY_FILTER_SCHEMA = (
    """
    ALTER TABLE discovered_jobs
        ADD COLUMN salary_daily_min_yuan INTEGER
        CHECK (salary_daily_min_yuan IS NULL OR salary_daily_min_yuan >= 0)
    """,
    """
    ALTER TABLE discovered_jobs
        ADD COLUMN salary_daily_max_yuan INTEGER
        CHECK (salary_daily_max_yuan IS NULL OR salary_daily_max_yuan >= 0)
    """,
    """
    ALTER TABLE discovered_jobs
        ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'other'
    """,
)

_RADAR_SCHEMA = (
    """
    ALTER TABLE discovery_run_jobs
        ADD COLUMN job_snapshot_json TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE discovery_run_jobs
        ADD COLUMN job_content_sha256 TEXT NOT NULL DEFAULT ''
    """,
    """
    CREATE TABLE radar_checks (
        run_id TEXT PRIMARY KEY,
        baseline_run_id TEXT NOT NULL,
        preference_fingerprint TEXT NOT NULL CHECK (length(preference_fingerprint) = 64),
        created_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (baseline_run_id) REFERENCES discovery_runs(run_id)
    )
    """,
    """
    CREATE TABLE radar_events (
        run_id TEXT NOT NULL,
        discovery_job_id TEXT NOT NULL,
        status TEXT NOT NULL,
        previous_content_sha256 TEXT,
        current_content_sha256 TEXT,
        job_snapshot_json TEXT NOT NULL,
        PRIMARY KEY (run_id, discovery_job_id),
        FOREIGN KEY (run_id) REFERENCES radar_checks(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_radar_checks_preference
        ON radar_checks(preference_fingerprint, created_at)
    """,
)


@dataclass(frozen=True)
class Migration:
    """One immutable embedded database migration."""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        """Return a stable checksum over version, name, and SQL statements."""
        payload = f"{self.version}\n{self.name}\n" + "\n-- statement --\n".join(
            statement.strip() for statement in self.statements
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationStatus:
    """Validated view of applied and pending migration versions."""

    applied_versions: tuple[int, ...]
    pending_versions: tuple[int, ...]
    latest_version: int

    @property
    def is_current(self) -> bool:
        """Return whether every known migration is applied."""
        return not self.pending_versions


MIGRATIONS = (
    Migration(1, "jobintel_initial", _INITIAL_SCHEMA),
    Migration(2, "jobintel_multi_source_discovery", _DISCOVERY_SCHEMA),
    Migration(3, "jobintel_boss_detail_enrichment", _DISCOVERY_DETAIL_SCHEMA),
    Migration(4, "jobintel_richer_discovery_filters", _DISCOVERY_FILTER_SCHEMA),
    Migration(5, "jobintel_incremental_radar", _RADAR_SCHEMA),
)


class MigrationRunner:
    """Apply and validate the embedded JobIntel migration chain."""

    def __init__(
        self, database: JobIntelDatabase, migrations: tuple[Migration, ...] = MIGRATIONS
    ) -> None:
        """Bind a migration chain to one database."""
        self._database = database
        self._migrations = migrations
        versions = tuple(migration.version for migration in migrations)
        if versions != tuple(range(1, len(migrations) + 1)):
            raise ValueError("migration versions must be contiguous and start at 1")

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._database.connection

    def _has_migration_table(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        return row is not None

    def _load_applied(self) -> list[sqlite3.Row]:
        if not self._has_migration_table():
            return []
        return list(
            self._conn.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        )

    def status(self) -> MigrationStatus:
        """Validate migration history and report applied and pending versions."""
        applied_rows = self._load_applied()
        known_by_version = {migration.version: migration for migration in self._migrations}
        applied_versions = tuple(int(row["version"]) for row in applied_rows)

        expected_prefix = tuple(range(1, len(applied_versions) + 1))
        if applied_versions != expected_prefix:
            raise MigrationHistoryError(
                f"migration history is not contiguous: {applied_versions!r}"
            )

        for row in applied_rows:
            version = int(row["version"])
            migration = known_by_version.get(version)
            if migration is None:
                raise MigrationHistoryError(
                    f"database migration version {version} is newer than this application"
                )
            if row["name"] != migration.name or row["checksum"] != migration.checksum:
                raise MigrationHistoryError(
                    f"migration {version} does not match its embedded name/checksum"
                )

        pending = tuple(
            migration.version
            for migration in self._migrations
            if migration.version not in applied_versions
        )
        latest = self._migrations[-1].version if self._migrations else 0
        return MigrationStatus(applied_versions, pending, latest)

    def migrate(self) -> MigrationStatus:
        """Apply all pending migrations, each in its own immediate transaction."""
        if not self._has_migration_table():
            self._conn.execute(_MIGRATION_TABLE_SQL)
            self._conn.commit()

        status = self.status()
        pending = set(status.pending_versions)
        for migration in self._migrations:
            if migration.version not in pending:
                continue
            with self._database.transaction() as connection:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        return self.status()

    def ensure_current(self) -> None:
        """Reject use of a database that has unapplied or invalid migrations."""
        status = self.status()
        if not self._has_migration_table() or not status.is_current:
            raise SchemaNotReadyError(
                f"JobIntel schema is not current; pending migrations: {status.pending_versions}"
            )
