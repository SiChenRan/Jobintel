"""Low-frequency incremental radar over successful, immutable discovery snapshots."""

from __future__ import annotations

from datetime import timedelta

from jobintel.discovery.models import (
    DiscoveryRun,
    RadarCheck,
    RadarEvent,
    RadarEventStatus,
    SourceStatus,
    discovered_job_content_sha256,
    preference_fingerprint,
    utc_now,
)
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import JobIntelError, RadarCooldownError
from jobintel.persistence.repository import SQLiteJobRepository


class JobRadarService:
    """Safely refresh one saved query and classify its job-level differences."""

    def __init__(
        self,
        repository: SQLiteJobRepository,
        discovery: JobDiscoveryService,
    ) -> None:
        """Bind persistence and the rate-limited live discovery service."""
        self._repository = repository
        self._discovery = discovery

    def check(
        self,
        baseline_run_id: str,
        *,
        cooldown_hours: int = 6,
        detail_limit: int = 0,
        detail_cache_hours: int = 24,
        force: bool = False,
    ) -> RadarCheck:
        """Run one successful refresh, persist it, and compare with the baseline."""
        if cooldown_hours < 1:
            raise ValueError("radar cooldown must be at least one hour")
        baseline = self._repository.get_discovery_run(baseline_run_id)
        fingerprint = preference_fingerprint(baseline.preference)
        latest = self._repository.latest_radar_check(fingerprint)
        if latest is not None and latest.run_id != baseline_run_id:
            raise ValueError(f"radar baseline is stale; use the latest check run: {latest.run_id}")
        previous_time = latest.created_at if latest is not None else baseline.created_at
        next_allowed = previous_time + timedelta(hours=cooldown_hours)
        now = utc_now()
        if not force and now < next_allowed:
            raise RadarCooldownError(
                "radar cooldown is active; next safe check is "
                f"{next_allowed.isoformat()} (use --force only for testing)"
            )

        current = self._discovery.discover(
            baseline.preference,
            persist=False,
            detail_limit=detail_limit,
            detail_cache_hours=detail_cache_hours,
        )
        failures = [
            attempt
            for attempt in current.source_attempts
            if attempt.status is not SourceStatus.SUCCESS
        ]
        if failures:
            summary = "; ".join(
                f"{attempt.source.value}:{attempt.status.value}" for attempt in failures
            )
            raise JobIntelError(f"radar source check was not successful: {summary}")
        if baseline.hits and not current.hits:
            raise JobIntelError(
                "radar returned zero jobs for a non-empty baseline; refusing mass-closed events"
            )

        saved_current = self._repository.save_discovery_run(current)
        check = self._compare(baseline, saved_current, fingerprint)
        return self._repository.save_radar_check(check)

    @staticmethod
    def _compare(baseline: DiscoveryRun, current: DiscoveryRun, fingerprint: str) -> RadarCheck:
        """Build a deterministic comparison after both runs have been validated."""
        previous = {hit.job.discovery_job_id: hit.job for hit in baseline.hits}
        latest = {hit.job.discovery_job_id: hit.job for hit in current.hits}
        events: list[RadarEvent] = []
        for job_id in sorted(previous.keys() | latest.keys()):
            old = previous.get(job_id)
            new = latest.get(job_id)
            old_hash = discovered_job_content_sha256(old) if old is not None else None
            new_hash = discovered_job_content_sha256(new) if new is not None else None
            if old is None:
                status = RadarEventStatus.NEW
                job = new
            elif new is None:
                status = RadarEventStatus.CLOSED
                job = old
            elif old_hash != new_hash:
                status = RadarEventStatus.CHANGED
                job = new
            else:
                status = RadarEventStatus.UNCHANGED
                job = new
            if job is None:  # pragma: no cover - set union guarantees one side
                raise RuntimeError("radar event has no job snapshot")
            events.append(
                RadarEvent(
                    discovery_job_id=job_id,
                    status=status,
                    job=job,
                    previous_content_sha256=old_hash,
                    current_content_sha256=new_hash,
                )
            )
        return RadarCheck(
            run_id=current.run_id,
            baseline_run_id=baseline.run_id,
            preference_fingerprint=fingerprint,
            events=tuple(events),
            created_at=current.created_at,
        )
