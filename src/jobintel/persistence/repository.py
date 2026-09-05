"""Typed SQLite repository for versioned JobIntel aggregates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from jobintel.discovery.models import (
    DetailAttempt,
    DiscoveredJob,
    DiscoveryHit,
    DiscoveryRun,
    JobSearchPreference,
    RadarCheck,
    RadarEvent,
    SourceAttempt,
    discovered_job_content_sha256,
)
from jobintel.errors import (
    EntityNotFoundError,
    IdempotencyConflictError,
    PersistenceValidationError,
)
from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    Company,
    JobAnalysis,
    JobPosting,
    JobRequirement,
)
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MigrationRunner


def _canonical_json(value: Any) -> str:
    """Serialize validated values deterministically for storage and hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _analysis_payload_sha256(analysis: JobAnalysis) -> str:
    """Hash semantic analysis content while excluding generated identity/time."""
    payload = _canonical_json(
        analysis.model_dump(mode="json", exclude={"analysis_id", "created_at"})
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_payload_sha256(job: JobPosting) -> str:
    """Hash semantic raw-job content while excluding intake time."""
    payload = _canonical_json(job.model_dump(mode="json", exclude={"created_at"}))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SQLiteJobRepository:
    """Map JobIntel domain aggregates to a current SQLite schema."""

    def __init__(self, database: JobIntelDatabase) -> None:
        """Bind to a migrated database and reject stale schemas."""
        MigrationRunner(database).ensure_current()
        self._database = database

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._database.connection

    # --- seed write path; transaction ownership remains with the caller -------

    def reset_all(self) -> None:
        """Delete all JobIntel domain rows without committing."""
        for table in (
            "radar_events",
            "radar_checks",
            "discovery_detail_attempts",
            "discovery_source_attempts",
            "discovery_run_jobs",
            "discovery_runs",
            "discovered_jobs",
            "requirement_match_evidence",
            "requirement_matches",
            "application_analyses",
            "candidate_evidence",
            "candidate_profiles",
            "requirements",
            "jobs",
            "companies",
        ):
            self._conn.execute(f"DELETE FROM {table}")

    # --- discovery aggregate read/write --------------------------------------

    def save_discovery_run(self, run: DiscoveryRun) -> DiscoveryRun:
        """Atomically persist a discovery run and update the canonical job cache."""
        try:
            with self._database.transaction():
                self.get_candidate_profile(
                    run.preference.candidate_id,
                    run.preference.profile_version,
                )
                self._conn.execute(
                    """
                    INSERT INTO discovery_runs (
                        run_id, candidate_id, profile_version, preference_json,
                        total_discovered, duplicates_removed, filtered_out,
                        schema_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.preference.candidate_id,
                        run.preference.profile_version,
                        _canonical_json(run.preference.model_dump(mode="json")),
                        run.total_discovered,
                        run.duplicates_removed,
                        run.filtered_out,
                        run.schema_version,
                        run.created_at.isoformat(),
                    ),
                )
                for attempt in run.source_attempts:
                    self._conn.execute(
                        """
                        INSERT INTO discovery_source_attempts (
                            run_id, source, status, discovered_count, elapsed_ms, message
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            attempt.source.value,
                            attempt.status.value,
                            attempt.discovered_count,
                            attempt.elapsed_ms,
                            attempt.message,
                        ),
                    )
                for rank_order, hit in enumerate(run.hits):
                    self._upsert_discovered_job(hit.job)
                    self._conn.execute(
                        """
                        INSERT INTO discovery_run_jobs (
                            run_id, discovery_job_id, rank_order, rank_score,
                            matched_terms_json, job_snapshot_json, job_content_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            hit.job.discovery_job_id,
                            rank_order,
                            hit.rank_score,
                            _canonical_json(hit.matched_terms),
                            _canonical_json(hit.job.model_dump(mode="json")),
                            discovered_job_content_sha256(hit.job),
                        ),
                    )
                for detail_attempt in run.detail_attempts:
                    self._conn.execute(
                        """
                        INSERT INTO discovery_detail_attempts (
                            run_id, discovery_job_id, source, external_id,
                            status, elapsed_ms, message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            detail_attempt.discovery_job_id,
                            detail_attempt.source.value,
                            detail_attempt.external_id,
                            detail_attempt.status.value,
                            detail_attempt.elapsed_ms,
                            detail_attempt.message,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"discovery aggregate violates database integrity: {run.run_id}"
            ) from exc
        return self.get_discovery_run(run.run_id)

    def _upsert_discovered_job(self, job: DiscoveredJob) -> None:
        """Insert a canonical job or refresh its current source snapshot."""
        self._conn.execute(
            """
            INSERT INTO discovered_jobs (
                discovery_job_id, canonical_key, title, company_name, location,
                salary_text, salary_min_k, salary_max_k, salary_daily_min_yuan,
                salary_daily_max_yuan, employment_type, description, experience,
                education, published_text, skills_json, company_description,
                recruiter_name, recruiter_title, recruiter_active_text,
                detail_fetched_at, detail_content_sha256, source_links_json,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discovery_job_id) DO UPDATE SET
                title = excluded.title,
                company_name = excluded.company_name,
                location = excluded.location,
                salary_text = excluded.salary_text,
                salary_min_k = excluded.salary_min_k,
                salary_max_k = excluded.salary_max_k,
                salary_daily_min_yuan = excluded.salary_daily_min_yuan,
                salary_daily_max_yuan = excluded.salary_daily_max_yuan,
                employment_type = excluded.employment_type,
                description = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL
                         OR discovered_jobs.detail_fetched_at IS NULL
                    THEN excluded.description
                    ELSE discovered_jobs.description
                END,
                experience = excluded.experience,
                education = excluded.education,
                published_text = excluded.published_text,
                skills_json = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL THEN excluded.skills_json
                    ELSE discovered_jobs.skills_json
                END,
                company_description = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL THEN excluded.company_description
                    ELSE discovered_jobs.company_description
                END,
                recruiter_name = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL THEN excluded.recruiter_name
                    ELSE discovered_jobs.recruiter_name
                END,
                recruiter_title = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL THEN excluded.recruiter_title
                    ELSE discovered_jobs.recruiter_title
                END,
                recruiter_active_text = CASE
                    WHEN excluded.detail_fetched_at IS NOT NULL
                    THEN excluded.recruiter_active_text
                    ELSE discovered_jobs.recruiter_active_text
                END,
                detail_fetched_at = COALESCE(
                    excluded.detail_fetched_at, discovered_jobs.detail_fetched_at
                ),
                detail_content_sha256 = COALESCE(
                    excluded.detail_content_sha256, discovered_jobs.detail_content_sha256
                ),
                source_links_json = excluded.source_links_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                job.discovery_job_id,
                job.canonical_key,
                job.title,
                job.company_name,
                job.location,
                job.salary_text,
                job.salary_min_k,
                job.salary_max_k,
                job.salary_daily_min_yuan,
                job.salary_daily_max_yuan,
                job.employment_type.value,
                job.description,
                job.experience,
                job.education,
                job.published_text,
                _canonical_json(job.skills),
                job.company_description,
                job.recruiter_name,
                job.recruiter_title,
                job.recruiter_active_text,
                job.detail_fetched_at.isoformat() if job.detail_fetched_at is not None else None,
                job.detail_content_sha256,
                _canonical_json([link.model_dump(mode="json") for link in job.source_links]),
                job.first_seen_at.isoformat(),
                job.last_seen_at.isoformat(),
            ),
        )

    def get_discovery_run(self, run_id: str) -> DiscoveryRun:
        """Rehydrate one persisted discovery run with ranked jobs and diagnostics."""
        row = self._conn.execute(
            "SELECT * FROM discovery_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"discovery run not found: {run_id}")
        attempt_rows = self._conn.execute(
            """
            SELECT source, status, discovered_count, elapsed_ms, message
            FROM discovery_source_attempts WHERE run_id = ? ORDER BY rowid
            """,
            (run_id,),
        ).fetchall()
        detail_attempt_rows = self._conn.execute(
            """
            SELECT discovery_job_id, source, external_id, status, elapsed_ms, message
            FROM discovery_detail_attempts WHERE run_id = ? ORDER BY rowid
            """,
            (run_id,),
        ).fetchall()
        hit_rows = self._conn.execute(
            """
            SELECT j.*, r.rank_score, r.matched_terms_json, r.job_snapshot_json
            FROM discovery_run_jobs r
            JOIN discovered_jobs j ON j.discovery_job_id = r.discovery_job_id
            WHERE r.run_id = ? ORDER BY r.rank_order
            """,
            (run_id,),
        ).fetchall()
        hits = []
        for hit_row in hit_rows:
            values = dict(hit_row)
            rank_score = values.pop("rank_score")
            matched_terms = json.loads(values.pop("matched_terms_json"))
            snapshot = values.pop("job_snapshot_json")
            if snapshot:
                job_values = json.loads(snapshot)
            else:
                values["source_links"] = json.loads(values.pop("source_links_json"))
                values["skills"] = json.loads(values.pop("skills_json"))
                job_values = values
            hits.append(
                DiscoveryHit(
                    job=DiscoveredJob.model_validate(job_values),
                    rank_score=rank_score,
                    matched_terms=matched_terms,
                )
            )
        return DiscoveryRun(
            run_id=row["run_id"],
            preference=JobSearchPreference.model_validate_json(row["preference_json"]),
            hits=tuple(hits),
            source_attempts=tuple(
                SourceAttempt.model_validate(dict(item)) for item in attempt_rows
            ),
            detail_attempts=tuple(
                DetailAttempt.model_validate(dict(item)) for item in detail_attempt_rows
            ),
            total_discovered=row["total_discovered"],
            duplicates_removed=row["duplicates_removed"],
            filtered_out=row["filtered_out"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    def list_discovery_runs(
        self, *, candidate_id: str | None = None, limit: int = 20
    ) -> tuple[DiscoveryRun, ...]:
        """Return newest discovery runs for dashboard and history views."""
        if not 1 <= limit <= 200:
            raise ValueError("discovery run list limit must be between 1 and 200")
        if candidate_id is None:
            rows = self._conn.execute(
                """
                SELECT run_id FROM discovery_runs
                ORDER BY created_at DESC, run_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT run_id FROM discovery_runs WHERE candidate_id = ?
                ORDER BY created_at DESC, run_id DESC LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return tuple(self.get_discovery_run(str(row["run_id"])) for row in rows)

    def find_discovered_job(self, discovery_job_id: str) -> DiscoveredJob | None:
        """Return a cached canonical job snapshot when it exists."""
        row = self._conn.execute(
            "SELECT * FROM discovered_jobs WHERE discovery_job_id = ?",
            (discovery_job_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["source_links"] = json.loads(values.pop("source_links_json"))
        values["skills"] = json.loads(values.pop("skills_json"))
        return DiscoveredJob.model_validate(values)

    def save_radar_check(self, check: RadarCheck) -> RadarCheck:
        """Atomically persist one already-computed radar comparison."""
        try:
            with self._database.transaction():
                self.get_discovery_run(check.run_id)
                self.get_discovery_run(check.baseline_run_id)
                self._conn.execute(
                    """
                    INSERT INTO radar_checks (
                        run_id, baseline_run_id, preference_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        check.run_id,
                        check.baseline_run_id,
                        check.preference_fingerprint,
                        check.created_at.isoformat(),
                    ),
                )
                for event in check.events:
                    self._conn.execute(
                        """
                        INSERT INTO radar_events (
                            run_id, discovery_job_id, status,
                            previous_content_sha256, current_content_sha256,
                            job_snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            check.run_id,
                            event.discovery_job_id,
                            event.status.value,
                            event.previous_content_sha256,
                            event.current_content_sha256,
                            _canonical_json(event.job.model_dump(mode="json")),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"radar check violates database integrity: {check.run_id}"
            ) from exc
        return self.get_radar_check(check.run_id)

    def get_radar_check(self, run_id: str) -> RadarCheck:
        """Rehydrate one radar comparison and its immutable job snapshots."""
        row = self._conn.execute(
            "SELECT * FROM radar_checks WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"radar check not found: {run_id}")
        event_rows = self._conn.execute(
            """
            SELECT * FROM radar_events WHERE run_id = ?
            ORDER BY CASE status
                WHEN 'new' THEN 0 WHEN 'changed' THEN 1
                WHEN 'closed' THEN 2 ELSE 3 END,
                discovery_job_id
            """,
            (run_id,),
        ).fetchall()
        events = tuple(
            RadarEvent(
                discovery_job_id=item["discovery_job_id"],
                status=item["status"],
                previous_content_sha256=item["previous_content_sha256"],
                current_content_sha256=item["current_content_sha256"],
                job=DiscoveredJob.model_validate_json(item["job_snapshot_json"]),
            )
            for item in event_rows
        )
        return RadarCheck(
            run_id=row["run_id"],
            baseline_run_id=row["baseline_run_id"],
            preference_fingerprint=row["preference_fingerprint"],
            events=events,
            created_at=row["created_at"],
        )

    def latest_radar_check(self, fingerprint: str) -> RadarCheck | None:
        """Return the newest check for a preference fingerprint."""
        row = self._conn.execute(
            """
            SELECT run_id FROM radar_checks WHERE preference_fingerprint = ?
            ORDER BY created_at DESC, run_id DESC LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
        return self.get_radar_check(row["run_id"]) if row is not None else None

    def list_radar_checks(
        self, *, candidate_id: str | None = None, limit: int = 20
    ) -> tuple[RadarCheck, ...]:
        """Return newest radar checks, optionally scoped through discovery ownership."""
        if not 1 <= limit <= 200:
            raise ValueError("radar check list limit must be between 1 and 200")
        sql = """
            SELECT rc.run_id FROM radar_checks AS rc
            JOIN discovery_runs AS dr ON dr.run_id = rc.run_id
        """
        parameters: tuple[object, ...]
        if candidate_id is None:
            sql += " ORDER BY rc.created_at DESC, rc.run_id DESC LIMIT ?"
            parameters = (limit,)
        else:
            sql += " WHERE dr.candidate_id = ?"
            sql += " ORDER BY rc.created_at DESC, rc.run_id DESC LIMIT ?"
            parameters = (candidate_id, limit)
        rows = self._conn.execute(sql, parameters).fetchall()
        return tuple(self.get_radar_check(str(row["run_id"])) for row in rows)

    def insert_company(self, company: Company) -> None:
        """Insert one company without committing."""
        self._conn.execute(
            """
            INSERT INTO companies (
                company_id, name, industry, headquarters, website, description
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                company.company_id,
                company.name,
                company.industry,
                company.headquarters,
                company.website,
                company.description,
            ),
        )

    def insert_job(self, job: JobPosting) -> None:
        """Insert one job version and all its requirements without committing."""
        self._conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_version, company_id, company_name, title, location,
                employment_type, description, source_url, source_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.job_version,
                job.company_id,
                job.company_name,
                job.title,
                job.location,
                job.employment_type,
                job.description,
                job.source_url,
                job.source_sha256,
                job.created_at.isoformat(),
            ),
        )
        for requirement in job.requirements:
            self._insert_requirement(job, requirement)

    def _insert_requirement(self, job: JobPosting, requirement: JobRequirement) -> None:
        """Insert one requirement under an already-inserted job version."""
        self._conn.execute(
            """
            INSERT INTO requirements (
                job_id, job_version, requirement_id, text, category, importance,
                normalized_skill, source_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.job_version,
                requirement.requirement_id,
                requirement.text,
                requirement.category.value,
                requirement.importance.value,
                requirement.normalized_skill,
                requirement.source_order,
            ),
        )

    def insert_candidate_profile(self, profile: CandidateProfile) -> None:
        """Insert one profile version and all evidence without committing."""
        self._conn.execute(
            """
            INSERT INTO candidate_profiles (
                candidate_id, profile_version, summary, source_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                profile.candidate_id,
                profile.profile_version,
                profile.summary,
                profile.source_sha256,
                profile.created_at.isoformat(),
            ),
        )
        for evidence in profile.evidence:
            self._insert_evidence(profile, evidence)

    def next_candidate_profile_version(self, candidate_id: str) -> int:
        """Return the next immutable profile version for a candidate."""
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(profile_version), 0) + 1
            FROM candidate_profiles WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        return int(row[0])

    def save_candidate_profile(self, profile: CandidateProfile) -> CandidateProfile:
        """Atomically append exactly the next candidate profile version."""
        try:
            with self._database.transaction():
                expected_version = self.next_candidate_profile_version(profile.candidate_id)
                if profile.profile_version != expected_version:
                    raise PersistenceValidationError(
                        "candidate profile preview is stale: "
                        f"expected version {expected_version}, got {profile.profile_version}"
                    )
                self.insert_candidate_profile(profile)
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                "candidate profile violates database integrity: "
                f"{profile.candidate_id}@{profile.profile_version}"
            ) from exc
        return self.get_candidate_profile(profile.candidate_id, profile.profile_version)

    def _insert_evidence(self, profile: CandidateProfile, evidence: CandidateEvidence) -> None:
        """Insert one evidence item under an already-inserted profile version."""
        self._conn.execute(
            """
            INSERT INTO candidate_evidence (
                candidate_id, profile_version, evidence_id, evidence_type, title,
                content, skills_json, source_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.candidate_id,
                profile.profile_version,
                evidence.evidence_id,
                evidence.evidence_type.value,
                evidence.title,
                evidence.content,
                _canonical_json(evidence.skills),
                evidence.source_order,
            ),
        )

    # --- typed read path -------------------------------------------------------

    def get_company(self, company_id: str) -> Company:
        """Return company context by ID."""
        row = self._conn.execute(
            "SELECT * FROM companies WHERE company_id = ?", (company_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"company not found: {company_id}")
        return Company.model_validate(dict(row))

    def get_job(self, job_id: str, job_version: int | None = None) -> JobPosting:
        """Return a requested job version or the latest immutable version."""
        if job_version is None:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? ORDER BY job_version DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND job_version = ?",
                (job_id, job_version),
            ).fetchone()
        if row is None:
            version_label = "latest" if job_version is None else str(job_version)
            raise EntityNotFoundError(f"job not found: {job_id}@{version_label}")

        requirement_rows = self._conn.execute(
            """
            SELECT requirement_id, text, category, importance, normalized_skill, source_order
            FROM requirements
            WHERE job_id = ? AND job_version = ?
            ORDER BY source_order, requirement_id
            """,
            (row["job_id"], row["job_version"]),
        ).fetchall()
        payload = dict(row)
        payload["requirements"] = [dict(requirement) for requirement in requirement_rows]
        return JobPosting.model_validate(payload)

    def get_candidate_profile(
        self, candidate_id: str, profile_version: int | None = None
    ) -> CandidateProfile:
        """Return a requested profile version or the latest immutable version."""
        if profile_version is None:
            row = self._conn.execute(
                """
                SELECT * FROM candidate_profiles
                WHERE candidate_id = ? ORDER BY profile_version DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM candidate_profiles
                WHERE candidate_id = ? AND profile_version = ?
                """,
                (candidate_id, profile_version),
            ).fetchone()
        if row is None:
            version_label = "latest" if profile_version is None else str(profile_version)
            raise EntityNotFoundError(
                f"candidate profile not found: {candidate_id}@{version_label}"
            )

        evidence_rows = self._conn.execute(
            """
            SELECT evidence_id, evidence_type, title, content, skills_json, source_order
            FROM candidate_evidence
            WHERE candidate_id = ? AND profile_version = ?
            ORDER BY source_order, evidence_id
            """,
            (row["candidate_id"], row["profile_version"]),
        ).fetchall()
        evidence = []
        for evidence_row in evidence_rows:
            item = dict(evidence_row)
            item["skills"] = json.loads(item.pop("skills_json"))
            evidence.append(item)
        payload = dict(row)
        payload["evidence"] = evidence
        return CandidateProfile.model_validate(payload)

    def list_candidate_profiles(self) -> tuple[CandidateProfile, ...]:
        """Return the latest immutable profile version for every candidate."""
        rows = self._conn.execute(
            """
            SELECT candidate_id, MAX(profile_version) AS profile_version
            FROM candidate_profiles GROUP BY candidate_id ORDER BY candidate_id
            """
        ).fetchall()
        return tuple(
            self.get_candidate_profile(str(row["candidate_id"]), int(row["profile_version"]))
            for row in rows
        )

    def dashboard_counts(self) -> dict[str, int]:
        """Return compact aggregate counts for the local web dashboard."""
        tables = {
            "candidates": "SELECT COUNT(DISTINCT candidate_id) FROM candidate_profiles",
            "discoveries": "SELECT COUNT(*) FROM discovery_runs",
            "jobs": "SELECT COUNT(*) FROM discovered_jobs",
            "analyses": "SELECT COUNT(*) FROM application_analyses",
            "radar_checks": "SELECT COUNT(*) FROM radar_checks",
        }
        return {
            name: int(self._conn.execute(statement).fetchone()[0])
            for name, statement in tables.items()
        }

    def get_analysis(self, analysis_id: str) -> JobAnalysis:
        """Rehydrate a complete analysis, matches, and evidence links."""
        row = self._conn.execute(
            "SELECT * FROM application_analyses WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"analysis not found: {analysis_id}")
        return self._analysis_from_row(row)

    def get_analysis_by_run_id(self, run_id: str) -> JobAnalysis:
        """Rehydrate a complete analysis by its idempotency key."""
        row = self._conn.execute(
            "SELECT * FROM application_analyses WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"analysis run not found: {run_id}")
        return self._analysis_from_row(row)

    def list_analyses(
        self, *, candidate_id: str | None = None, limit: int = 50
    ) -> tuple[JobAnalysis, ...]:
        """Return newest persisted analyses, optionally scoped to one candidate."""
        if not 1 <= limit <= 500:
            raise ValueError("analysis list limit must be between 1 and 500")
        if candidate_id is None:
            rows = self._conn.execute(
                """
                SELECT * FROM application_analyses
                ORDER BY created_at DESC, analysis_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM application_analyses WHERE candidate_id = ?
                ORDER BY created_at DESC, analysis_id DESC LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return tuple(self._analysis_from_row(row) for row in rows)

    def _analysis_from_row(self, row: sqlite3.Row) -> JobAnalysis:
        """Map one parent row plus normalized children into a domain aggregate."""
        match_rows = self._conn.execute(
            """
            SELECT rm.requirement_id, rm.status, rm.confidence, rm.reason
            FROM requirement_matches AS rm
            JOIN requirements AS r
              ON r.job_id = rm.job_id
             AND r.job_version = rm.job_version
             AND r.requirement_id = rm.requirement_id
            WHERE rm.analysis_id = ?
            ORDER BY r.source_order, rm.requirement_id
            """,
            (row["analysis_id"],),
        ).fetchall()
        matches = []
        for match_row in match_rows:
            evidence_rows = self._conn.execute(
                """
                SELECT evidence_id FROM requirement_match_evidence
                WHERE analysis_id = ? AND requirement_id = ?
                ORDER BY citation_order, evidence_id
                """,
                (row["analysis_id"], match_row["requirement_id"]),
            ).fetchall()
            match = dict(match_row)
            match["evidence_ids"] = [item["evidence_id"] for item in evidence_rows]
            matches.append(match)

        payload = dict(row)
        payload.pop("payload_sha256")
        for column in (
            "score_breakdown_json",
            "strengths_json",
            "resume_suggestions_json",
            "interview_topics_json",
            "missing_skills_json",
        ):
            payload[column.removesuffix("_json")] = json.loads(payload.pop(column))
        payload["requirement_matches"] = matches
        return JobAnalysis.model_validate(payload)

    # --- atomic analysis aggregate write --------------------------------------

    def save_analysis(self, analysis: JobAnalysis) -> JobAnalysis:
        """Validate and atomically persist an analysis aggregate.

        Replaying the same canonical payload under the same ``run_id`` returns
        the existing aggregate. Reusing that key for any changed payload is a
        deterministic conflict.
        """
        payload_sha256 = _analysis_payload_sha256(analysis)
        try:
            with self._database.transaction():
                existing = self._existing_analysis(analysis, payload_sha256)
                if existing is not None:
                    return existing
                self._save_analysis_rows(analysis, payload_sha256)
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"analysis aggregate violates database integrity: {analysis.analysis_id}"
            ) from exc
        return self.get_analysis(analysis.analysis_id)

    def save_staged_job_analysis(self, job: JobPosting, analysis: JobAnalysis) -> JobAnalysis:
        """Atomically persist a staged raw-JD job and its finalized analysis."""
        if (job.job_id, job.job_version) != (analysis.job_id, analysis.job_version):
            raise PersistenceValidationError(
                "staged job identity does not match analysis job identity"
            )
        payload_sha256 = _analysis_payload_sha256(analysis)
        try:
            with self._database.transaction():
                existing_analysis = self._existing_analysis(analysis, payload_sha256)
                if existing_analysis is not None:
                    return existing_analysis
                try:
                    existing_job = self.get_job(job.job_id, job.job_version)
                except EntityNotFoundError:
                    self.insert_job(job)
                else:
                    if _job_payload_sha256(existing_job) != _job_payload_sha256(job):
                        raise IdempotencyConflictError(
                            "raw job identity already exists with different content: "
                            f"{job.job_id}@{job.job_version}"
                        )
                self._save_analysis_rows(analysis, payload_sha256)
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"staged job analysis violates database integrity: {analysis.analysis_id}"
            ) from exc
        return self.get_analysis(analysis.analysis_id)

    def _existing_analysis(self, analysis: JobAnalysis, payload_sha256: str) -> JobAnalysis | None:
        """Resolve an idempotent run or reject reuse with changed semantics."""
        existing = self._conn.execute(
            """
            SELECT analysis_id, payload_sha256 FROM application_analyses
            WHERE run_id = ?
            """,
            (analysis.run_id,),
        ).fetchone()
        if existing is None:
            return None
        if existing["payload_sha256"] != payload_sha256:
            raise IdempotencyConflictError(
                f"run_id already exists with different payload: {analysis.run_id}"
            )
        return self.get_analysis(str(existing["analysis_id"]))

    def _save_analysis_rows(self, analysis: JobAnalysis, payload_sha256: str) -> None:
        """Validate and insert all analysis rows inside the caller transaction."""
        self._validate_analysis_scope(analysis)
        self._insert_analysis(analysis, payload_sha256)
        self._insert_matches(analysis)

    def _validate_analysis_scope(self, analysis: JobAnalysis) -> None:
        """Recheck version, requirement, and evidence ownership before writing."""
        job = self.get_job(analysis.job_id, analysis.job_version)
        self.get_candidate_profile(analysis.candidate_id, analysis.profile_version)
        requirement_ids = {item.requirement_id for item in job.requirements}
        matched_ids = {item.requirement_id for item in analysis.requirement_matches}
        if matched_ids != requirement_ids:
            missing = sorted(requirement_ids - matched_ids)
            unknown = sorted(matched_ids - requirement_ids)
            raise PersistenceValidationError(
                f"analysis requirement scope mismatch; missing={missing}, unknown={unknown}"
            )

        evidence_by_match = {
            match.requirement_id: set(match.evidence_ids) for match in analysis.requirement_matches
        }
        cited_evidence = set().union(*evidence_by_match.values()) if evidence_by_match else set()
        for evidence_id in sorted(cited_evidence):
            row = self._conn.execute(
                """
                SELECT 1 FROM candidate_evidence
                WHERE candidate_id = ? AND profile_version = ? AND evidence_id = ?
                """,
                (analysis.candidate_id, analysis.profile_version, evidence_id),
            ).fetchone()
            if row is None:
                raise PersistenceValidationError(
                    f"analysis evidence is outside candidate/profile scope: {evidence_id}"
                )

        for strength in analysis.strengths:
            if not set(strength.requirement_ids) <= requirement_ids:
                raise PersistenceValidationError(
                    f"strength references unknown requirement: {strength.claim_id}"
                )
            allowed_evidence = set().union(
                *(evidence_by_match[item] for item in strength.requirement_ids)
            )
            if not set(strength.evidence_ids) <= allowed_evidence:
                raise PersistenceValidationError(
                    f"strength references evidence outside its matches: {strength.claim_id}"
                )

        referenced_requirements = {
            item.requirement_id
            for collection in (
                analysis.resume_suggestions,
                analysis.interview_topics,
                analysis.missing_skills,
            )
            for item in collection
        }
        if not referenced_requirements <= requirement_ids:
            unknown = sorted(referenced_requirements - requirement_ids)
            raise PersistenceValidationError(
                f"analysis narrative references unknown requirements: {unknown}"
            )

    def _insert_analysis(self, analysis: JobAnalysis, payload_sha256: str) -> None:
        """Insert the parent analysis row without committing."""
        self._conn.execute(
            """
            INSERT INTO application_analyses (
                analysis_id, run_id, job_id, job_version, candidate_id, profile_version,
                score, recommendation, score_breakdown_json, strengths_json,
                resume_suggestions_json, interview_topics_json, missing_skills_json,
                next_action, prompt_version, parser_version, toolset_version,
                scoring_version, schema_version, provenance_version, provenance_digest,
                payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.analysis_id,
                analysis.run_id,
                analysis.job_id,
                analysis.job_version,
                analysis.candidate_id,
                analysis.profile_version,
                analysis.score,
                analysis.recommendation.value,
                _canonical_json(analysis.score_breakdown.model_dump(mode="json")),
                _canonical_json([item.model_dump(mode="json") for item in analysis.strengths]),
                _canonical_json(
                    [item.model_dump(mode="json") for item in analysis.resume_suggestions]
                ),
                _canonical_json(
                    [item.model_dump(mode="json") for item in analysis.interview_topics]
                ),
                _canonical_json([item.model_dump(mode="json") for item in analysis.missing_skills]),
                analysis.next_action,
                analysis.prompt_version,
                analysis.parser_version,
                analysis.toolset_version,
                analysis.scoring_version,
                analysis.schema_version,
                analysis.provenance_version,
                analysis.provenance_digest,
                payload_sha256,
                analysis.created_at.isoformat(),
            ),
        )

    def _insert_matches(self, analysis: JobAnalysis) -> None:
        """Insert normalized requirement matches and evidence citations."""
        for match in analysis.requirement_matches:
            self._conn.execute(
                """
                INSERT INTO requirement_matches (
                    analysis_id, requirement_id, job_id, job_version, candidate_id,
                    profile_version, status, confidence, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis.analysis_id,
                    match.requirement_id,
                    analysis.job_id,
                    analysis.job_version,
                    analysis.candidate_id,
                    analysis.profile_version,
                    match.status.value,
                    match.confidence,
                    match.reason,
                ),
            )
            for citation_order, evidence_id in enumerate(match.evidence_ids):
                self._conn.execute(
                    """
                    INSERT INTO requirement_match_evidence (
                        analysis_id, requirement_id, candidate_id, profile_version,
                        evidence_id, citation_order
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        analysis.analysis_id,
                        match.requirement_id,
                        analysis.candidate_id,
                        analysis.profile_version,
                        evidence_id,
                        citation_order,
                    ),
                )
