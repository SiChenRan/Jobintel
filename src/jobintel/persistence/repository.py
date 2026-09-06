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
from jobintel.notifications.models import (
    CandidateEmailPreference,
    EmailNotificationAttempt,
    EmailNotificationStatus,
)
from jobintel.outreach.models import (
    OutreachClaim,
    OutreachDraft,
    OutreachEvent,
    OutreachEventAttribute,
    OutreachEventType,
    OutreachStatus,
)
from jobintel.outreach.state import validate_outreach_event
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


def _outreach_payload_sha256(draft: OutreachDraft) -> str:
    """Hash immutable revision content while excluding lifecycle timestamps/status."""
    payload = _canonical_json(
        draft.model_dump(mode="json", exclude={"status", "created_at", "updated_at"})
    )
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
            "email_notification_attempts",
            "candidate_email_preferences",
            "outreach_events",
            "outreach_claim_evidence",
            "outreach_claim_requirements",
            "outreach_claims",
            "outreach_drafts",
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
                        profile_snapshot_json, schema_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.preference.candidate_id,
                        run.preference.profile_version,
                        _canonical_json(run.preference.model_dump(mode="json")),
                        run.total_discovered,
                        run.duplicates_removed,
                        run.filtered_out,
                        _canonical_json(
                            run.profile_snapshot.model_dump(mode="json")
                            if run.profile_snapshot is not None
                            else {}
                        ),
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
                            matched_terms_json, rank_explanation_json,
                            job_snapshot_json, job_content_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            hit.job.discovery_job_id,
                            rank_order,
                            hit.rank_score,
                            _canonical_json(hit.matched_terms),
                            _canonical_json(
                                {
                                    "matched_query_terms": hit.matched_query_terms,
                                    "matched_profile_skills": hit.matched_profile_skills,
                                    "matched_evidence": [
                                        item.model_dump(mode="json")
                                        for item in hit.matched_evidence
                                    ],
                                    "rank_breakdown": (
                                        hit.rank_breakdown.model_dump(mode="json")
                                        if hit.rank_breakdown is not None
                                        else None
                                    ),
                                    "is_new_to_candidate": hit.is_new_to_candidate,
                                }
                            ),
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
                salary_daily_max_yuan, employment_type, company_size, description, experience,
                education, published_text, skills_json, company_description,
                recruiter_name, recruiter_title, recruiter_active_text,
                detail_fetched_at, detail_content_sha256, source_links_json,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                company_size = excluded.company_size,
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
                job.company_size.value,
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
            SELECT j.*, r.rank_score, r.matched_terms_json,
                   r.rank_explanation_json, r.job_snapshot_json
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
            rank_explanation = json.loads(values.pop("rank_explanation_json"))
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
                    **rank_explanation,
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
            profile_snapshot=(
                json.loads(row["profile_snapshot_json"])
                if row["profile_snapshot_json"] != "{}"
                else None
            ),
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

    def list_candidate_seen_discovery_job_ids(self, candidate_id: str) -> tuple[str, ...]:
        """Return canonical jobs already shown in persisted runs for one candidate."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT rj.discovery_job_id
            FROM discovery_run_jobs AS rj
            JOIN discovery_runs AS dr ON dr.run_id = rj.run_id
            WHERE dr.candidate_id = ?
            ORDER BY rj.discovery_job_id
            """,
            (candidate_id,),
        ).fetchall()
        return tuple(str(row["discovery_job_id"]) for row in rows)

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

    def find_recruiter_name_for_source_url(self, source_url: str | None) -> str | None:
        """Return an observed recruiter name for an exact cached source URL."""
        if not source_url:
            return None
        rows = self._conn.execute(
            """
            SELECT recruiter_name, source_links_json FROM discovered_jobs
            WHERE recruiter_name != '' ORDER BY last_seen_at DESC
            """
        ).fetchall()
        for row in rows:
            links = json.loads(row["source_links_json"])
            if any(str(item.get("url", "")) == source_url for item in links):
                return str(row["recruiter_name"])
        return None

    def save_candidate_email_preference(
        self, preference: CandidateEmailPreference
    ) -> CandidateEmailPreference:
        """Create or replace one candidate-scoped notification recipient."""
        try:
            with self._database.transaction():
                self.get_candidate_profile(preference.candidate_id)
                self._conn.execute(
                    """
                    INSERT INTO candidate_email_preferences (
                        candidate_id, recipient_email, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        recipient_email = excluded.recipient_email,
                        updated_at = excluded.updated_at
                    """,
                    (
                        preference.candidate_id,
                        preference.recipient_email,
                        preference.created_at.isoformat(),
                        preference.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"candidate email preference violates database integrity: {preference.candidate_id}"
            ) from exc
        return self.get_candidate_email_preference(preference.candidate_id)

    def find_candidate_email_preference(self, candidate_id: str) -> CandidateEmailPreference | None:
        """Return a candidate's recipient setting when configured."""
        row = self._conn.execute(
            "SELECT * FROM candidate_email_preferences WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return CandidateEmailPreference.model_validate(dict(row))

    def get_candidate_email_preference(self, candidate_id: str) -> CandidateEmailPreference:
        """Return a candidate's recipient setting or a stable not-found error."""
        preference = self.find_candidate_email_preference(candidate_id)
        if preference is None:
            raise EntityNotFoundError(
                f"candidate notification email not configured: {candidate_id}"
            )
        return preference

    def save_email_notification_attempt(
        self, attempt: EmailNotificationAttempt
    ) -> EmailNotificationAttempt:
        """Persist one pending delivery marker before external SMTP activity."""
        if attempt.status is not EmailNotificationStatus.PENDING:
            raise PersistenceValidationError("new email notification attempt must be pending")
        try:
            with self._database.transaction():
                self.get_discovery_run(attempt.discovery_run_id)
                self._conn.execute(
                    """
                    INSERT INTO email_notification_attempts (
                        notification_id, discovery_run_id, recipient_masked,
                        job_count, status, subject_sha256, error_code,
                        created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.notification_id,
                        attempt.discovery_run_id,
                        attempt.recipient_masked,
                        attempt.job_count,
                        attempt.status.value,
                        attempt.subject_sha256,
                        attempt.error_code,
                        attempt.created_at.isoformat(),
                        None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"email notification attempt violates database integrity: {attempt.notification_id}"
            ) from exc
        return self.get_email_notification_attempt(attempt.notification_id)

    def complete_email_notification_attempt(
        self, attempt: EmailNotificationAttempt
    ) -> EmailNotificationAttempt:
        """Atomically finalize one previously pending delivery attempt."""
        if attempt.status is EmailNotificationStatus.PENDING:
            raise PersistenceValidationError("completed email notification cannot be pending")
        current = self.get_email_notification_attempt(attempt.notification_id)
        immutable_fields = (
            "discovery_run_id",
            "recipient_masked",
            "job_count",
            "subject_sha256",
            "created_at",
        )
        if any(getattr(current, field) != getattr(attempt, field) for field in immutable_fields):
            raise IdempotencyConflictError(
                f"email notification identity changed: {attempt.notification_id}"
            )
        if current.status is not EmailNotificationStatus.PENDING:
            if current == attempt:
                return current
            raise IdempotencyConflictError(
                f"email notification already completed: {attempt.notification_id}"
            )
        with self._database.transaction():
            cursor = self._conn.execute(
                """
                UPDATE email_notification_attempts
                SET status = ?, error_code = ?, completed_at = ?
                WHERE notification_id = ? AND status = ?
                """,
                (
                    attempt.status.value,
                    attempt.error_code,
                    attempt.completed_at.isoformat() if attempt.completed_at else None,
                    attempt.notification_id,
                    EmailNotificationStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise IdempotencyConflictError(
                    f"email notification changed concurrently: {attempt.notification_id}"
                )
        return self.get_email_notification_attempt(attempt.notification_id)

    def get_email_notification_attempt(self, notification_id: str) -> EmailNotificationAttempt:
        """Return one content-minimal delivery audit record."""
        row = self._conn.execute(
            "SELECT * FROM email_notification_attempts WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"email notification not found: {notification_id}")
        return EmailNotificationAttempt.model_validate(dict(row))

    def list_email_notification_attempts(
        self, *, discovery_run_id: str | None = None, limit: int = 50
    ) -> tuple[EmailNotificationAttempt, ...]:
        """List recent delivery receipts without message bodies or raw addresses."""
        if not 1 <= limit <= 500:
            raise ValueError("email notification list limit must be between 1 and 500")
        if discovery_run_id is None:
            rows = self._conn.execute(
                """
                SELECT * FROM email_notification_attempts
                ORDER BY created_at DESC, notification_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM email_notification_attempts WHERE discovery_run_id = ?
                ORDER BY created_at DESC, notification_id DESC LIMIT ?
                """,
                (discovery_run_id, limit),
            ).fetchall()
        return tuple(EmailNotificationAttempt.model_validate(dict(row)) for row in rows)

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

    def dashboard_counts(self, *, candidate_id: str | None = None) -> dict[str, int]:
        """Return aggregate counts globally or within one candidate's data scope."""
        if candidate_id is None:
            statements: dict[str, tuple[str, tuple[object, ...]]] = {
                "candidates": (
                    "SELECT COUNT(DISTINCT candidate_id) FROM candidate_profiles",
                    (),
                ),
                "discoveries": ("SELECT COUNT(*) FROM discovery_runs", ()),
                "jobs": ("SELECT COUNT(*) FROM discovered_jobs", ()),
                "analyses": ("SELECT COUNT(*) FROM application_analyses", ()),
                "radar_checks": ("SELECT COUNT(*) FROM radar_checks", ()),
                "outreach_drafts": ("SELECT COUNT(*) FROM outreach_drafts", ()),
            }
        else:
            parameter = (candidate_id,)
            statements = {
                "candidates": (
                    "SELECT COUNT(DISTINCT candidate_id) FROM candidate_profiles "
                    "WHERE candidate_id = ?",
                    parameter,
                ),
                "discoveries": (
                    "SELECT COUNT(*) FROM discovery_runs WHERE candidate_id = ?",
                    parameter,
                ),
                "jobs": (
                    """
                    SELECT COUNT(DISTINCT rj.discovery_job_id)
                    FROM discovery_run_jobs AS rj
                    JOIN discovery_runs AS r ON r.run_id = rj.run_id
                    WHERE r.candidate_id = ?
                    """,
                    parameter,
                ),
                "analyses": (
                    "SELECT COUNT(*) FROM application_analyses WHERE candidate_id = ?",
                    parameter,
                ),
                "radar_checks": (
                    """
                    SELECT COUNT(*) FROM radar_checks AS rc
                    JOIN discovery_runs AS r ON r.run_id = rc.run_id
                    WHERE r.candidate_id = ?
                    """,
                    parameter,
                ),
                "outreach_drafts": (
                    "SELECT COUNT(*) FROM outreach_drafts WHERE candidate_id = ?",
                    parameter,
                ),
            }
        return {
            name: int(self._conn.execute(statement, parameters).fetchone()[0])
            for name, (statement, parameters) in statements.items()
        }

    # --- reviewed HR outreach ------------------------------------------------

    def save_outreach(self, draft: OutreachDraft) -> OutreachDraft:
        """Atomically persist one immutable draft revision with normalized citations."""
        payload_sha256 = _outreach_payload_sha256(draft)
        try:
            with self._database.transaction():
                existing = self._conn.execute(
                    """
                    SELECT payload_sha256 FROM outreach_drafts
                    WHERE outreach_id = ? AND revision = ?
                    """,
                    (draft.outreach_id, draft.revision),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256:
                        raise IdempotencyConflictError(
                            "outreach revision already exists with different content: "
                            f"{draft.outreach_id}@{draft.revision}"
                        )
                    return self.get_outreach(draft.outreach_id, draft.revision)
                self._validate_outreach_scope(draft)
                self._insert_outreach_rows(draft, payload_sha256)
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                "outreach aggregate violates database integrity: "
                f"{draft.outreach_id}@{draft.revision}"
            ) from exc
        return self.get_outreach(draft.outreach_id, draft.revision)

    def _validate_outreach_scope(self, draft: OutreachDraft) -> None:
        """Recheck analysis, job, profile, requirement, and evidence ownership."""
        analysis = self.get_analysis(draft.analysis_id)
        expected_scope = (
            analysis.job_id,
            analysis.job_version,
            analysis.candidate_id,
            analysis.profile_version,
        )
        actual_scope = (
            draft.job_id,
            draft.job_version,
            draft.candidate_id,
            draft.profile_version,
        )
        if actual_scope != expected_scope:
            raise PersistenceValidationError("outreach scope does not match its analysis")
        job = self.get_job(draft.job_id, draft.job_version)
        profile = self.get_candidate_profile(draft.candidate_id, draft.profile_version)
        requirement_ids = {item.requirement_id for item in job.requirements}
        evidence_ids = {item.evidence_id for item in profile.evidence}
        for claim in draft.claims:
            if not set(claim.requirement_ids) <= requirement_ids:
                raise PersistenceValidationError(
                    f"outreach claim references unknown requirement: {claim.claim_id}"
                )
            if not set(claim.evidence_ids) <= evidence_ids:
                raise PersistenceValidationError(
                    f"outreach claim references unknown evidence: {claim.claim_id}"
                )

    def _insert_outreach_rows(self, draft: OutreachDraft, payload_sha256: str) -> None:
        """Insert one already-validated outreach revision inside a transaction."""
        self._conn.execute(
            """
            INSERT INTO outreach_drafts (
                outreach_id, revision, analysis_id, job_id, job_version,
                candidate_id, profile_version, channel, tone, salutation,
                motivation, conversation_opener, closing, rendered_message,
                user_edited_message, status, provider, prompt_version,
                schema_version, provenance_digest, payload_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.outreach_id,
                draft.revision,
                draft.analysis_id,
                draft.job_id,
                draft.job_version,
                draft.candidate_id,
                draft.profile_version,
                draft.channel.value,
                draft.tone.value,
                draft.salutation,
                draft.motivation,
                draft.conversation_opener,
                draft.closing,
                draft.rendered_message,
                draft.user_edited_message,
                draft.status.value,
                draft.provider,
                draft.prompt_version,
                draft.schema_version,
                draft.provenance_digest,
                payload_sha256,
                draft.created_at.isoformat(),
                draft.updated_at.isoformat(),
            ),
        )
        for claim in draft.claims:
            self._conn.execute(
                """
                INSERT INTO outreach_claims
                    (outreach_id, revision, claim_id, source_order, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    draft.outreach_id,
                    draft.revision,
                    claim.claim_id,
                    claim.source_order,
                    claim.text,
                ),
            )
            for order, requirement_id in enumerate(claim.requirement_ids):
                self._conn.execute(
                    """
                    INSERT INTO outreach_claim_requirements (
                        outreach_id, revision, claim_id, requirement_id,
                        reference_order, job_id, job_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.outreach_id,
                        draft.revision,
                        claim.claim_id,
                        requirement_id,
                        order,
                        draft.job_id,
                        draft.job_version,
                    ),
                )
            for order, evidence_id in enumerate(claim.evidence_ids):
                self._conn.execute(
                    """
                    INSERT INTO outreach_claim_evidence (
                        outreach_id, revision, claim_id, evidence_id,
                        citation_order, candidate_id, profile_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.outreach_id,
                        draft.revision,
                        claim.claim_id,
                        evidence_id,
                        order,
                        draft.candidate_id,
                        draft.profile_version,
                    ),
                )

    def get_outreach(self, outreach_id: str, revision: int | None = None) -> OutreachDraft:
        """Return one exact outreach revision or the newest revision when omitted."""
        if revision is None:
            row = self._conn.execute(
                """
                SELECT * FROM outreach_drafts WHERE outreach_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (outreach_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM outreach_drafts WHERE outreach_id = ? AND revision = ?
                """,
                (outreach_id, revision),
            ).fetchone()
        if row is None:
            suffix = "latest" if revision is None else str(revision)
            raise EntityNotFoundError(f"outreach not found: {outreach_id}@{suffix}")
        return self._outreach_from_row(row)

    def _outreach_from_row(self, row: sqlite3.Row) -> OutreachDraft:
        """Rehydrate a draft revision and its ordered claims/citations."""
        claim_rows = self._conn.execute(
            """
            SELECT * FROM outreach_claims
            WHERE outreach_id = ? AND revision = ? ORDER BY source_order
            """,
            (row["outreach_id"], row["revision"]),
        ).fetchall()
        claims: list[OutreachClaim] = []
        for claim_row in claim_rows:
            requirement_rows = self._conn.execute(
                """
                SELECT requirement_id FROM outreach_claim_requirements
                WHERE outreach_id = ? AND revision = ? AND claim_id = ?
                ORDER BY reference_order
                """,
                (row["outreach_id"], row["revision"], claim_row["claim_id"]),
            ).fetchall()
            evidence_rows = self._conn.execute(
                """
                SELECT evidence_id FROM outreach_claim_evidence
                WHERE outreach_id = ? AND revision = ? AND claim_id = ?
                ORDER BY citation_order
                """,
                (row["outreach_id"], row["revision"], claim_row["claim_id"]),
            ).fetchall()
            claims.append(
                OutreachClaim(
                    claim_id=claim_row["claim_id"],
                    source_order=claim_row["source_order"],
                    text=claim_row["text"],
                    requirement_ids=tuple(item["requirement_id"] for item in requirement_rows),
                    evidence_ids=tuple(item["evidence_id"] for item in evidence_rows),
                )
            )
        payload = dict(row)
        payload.pop("payload_sha256")
        payload["claims"] = claims
        return OutreachDraft.model_validate(payload)

    def list_outreach(
        self, *, candidate_id: str | None = None, limit: int = 50
    ) -> tuple[OutreachDraft, ...]:
        """Return newest revision of each outreach, optionally for one candidate."""
        if not 1 <= limit <= 500:
            raise ValueError("outreach list limit must be between 1 and 500")
        parameters: tuple[object, ...]
        where = ""
        if candidate_id is None:
            parameters = (limit,)
        else:
            where = "WHERE d.candidate_id = ?"
            parameters = (candidate_id, limit)
        rows = self._conn.execute(
            f"""
            SELECT d.* FROM outreach_drafts AS d
            JOIN (
                SELECT outreach_id, MAX(revision) AS revision
                FROM outreach_drafts GROUP BY outreach_id
            ) AS latest
              ON latest.outreach_id = d.outreach_id AND latest.revision = d.revision
            {where}
            ORDER BY d.updated_at DESC, d.outreach_id DESC LIMIT ?
            """,
            parameters,
        ).fetchall()
        return tuple(self._outreach_from_row(row) for row in rows)

    def list_outreach_for_analysis(self, analysis_id: str) -> tuple[OutreachDraft, ...]:
        """Return newest revision of every draft generated for one analysis."""
        rows = self._conn.execute(
            """
            SELECT d.* FROM outreach_drafts AS d
            JOIN (
                SELECT outreach_id, MAX(revision) AS revision
                FROM outreach_drafts WHERE analysis_id = ? GROUP BY outreach_id
            ) AS latest
              ON latest.outreach_id = d.outreach_id AND latest.revision = d.revision
            WHERE d.analysis_id = ?
            ORDER BY d.updated_at DESC, d.outreach_id DESC
            """,
            (analysis_id, analysis_id),
        ).fetchall()
        return tuple(self._outreach_from_row(row) for row in rows)

    def apply_outreach_event(self, event: OutreachEvent) -> OutreachDraft:
        """Atomically append one action event and apply its validated state change."""
        try:
            with self._database.transaction():
                existing = self._conn.execute(
                    "SELECT * FROM outreach_events WHERE event_id = ?", (event.event_id,)
                ).fetchone()
                if existing is not None:
                    stored = self._outreach_event_from_row(existing)
                    if stored != event:
                        raise IdempotencyConflictError(
                            f"outreach event key already exists: {event.event_id}"
                        )
                    return self.get_outreach(event.outreach_id, event.revision)
                draft = self.get_outreach(event.outreach_id, event.revision)
                latest = self.get_outreach(event.outreach_id)
                if latest.revision != event.revision:
                    raise IdempotencyConflictError(
                        f"outreach revision is stale: {event.outreach_id}@{event.revision}"
                    )
                if draft.status is not event.from_status:
                    raise IdempotencyConflictError(
                        f"outreach status changed: expected {event.from_status.value}, "
                        f"found {draft.status.value}"
                    )
                validate_outreach_event(draft.status, event.event_type)
                expected_target = {
                    OutreachEventType.APPROVED: OutreachStatus.APPROVED,
                    OutreachEventType.SENT_CONFIRMED: OutreachStatus.SENT_CONFIRMED,
                    OutreachEventType.DISMISSED: OutreachStatus.DISMISSED,
                    OutreachEventType.COPIED: draft.status,
                    OutreachEventType.OPENED: draft.status,
                }[event.event_type]
                if event.to_status is not expected_target:
                    raise PersistenceValidationError("outreach event target status is invalid")
                self._conn.execute(
                    """
                    UPDATE outreach_drafts SET status = ?, updated_at = ?
                    WHERE outreach_id = ? AND revision = ?
                    """,
                    (
                        event.to_status.value,
                        event.created_at.isoformat(),
                        event.outreach_id,
                        event.revision,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO outreach_events (
                        event_id, outreach_id, revision, event_type, from_status,
                        to_status, attributes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.outreach_id,
                        event.revision,
                        event.event_type.value,
                        event.from_status.value,
                        event.to_status.value,
                        _canonical_json(
                            [item.model_dump(mode="json") for item in event.attributes]
                        ),
                        event.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceValidationError(
                f"outreach event violates database integrity: {event.event_id}"
            ) from exc
        return self.get_outreach(event.outreach_id, event.revision)

    def list_outreach_events(self, outreach_id: str) -> tuple[OutreachEvent, ...]:
        """Return the append-only audit history for one outreach."""
        rows = self._conn.execute(
            """
            SELECT * FROM outreach_events WHERE outreach_id = ?
            ORDER BY created_at, event_id
            """,
            (outreach_id,),
        ).fetchall()
        return tuple(self._outreach_event_from_row(row) for row in rows)

    @staticmethod
    def _outreach_event_from_row(row: sqlite3.Row) -> OutreachEvent:
        payload = dict(row)
        payload["attributes"] = tuple(
            OutreachEventAttribute.model_validate(item)
            for item in json.loads(payload.pop("attributes_json"))
        )
        return OutreachEvent.model_validate(payload)

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
