"""Concurrent discovery, cross-platform deduplication, filtering, and ranking."""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from time import perf_counter

from jobintel.discovery.connectors.base import (
    AuthenticationRequiredError,
    JobDetailConnector,
    JobSourceConnector,
    SourceBlockedError,
    SourceUnavailableError,
)
from jobintel.discovery.models import (
    CompanySize,
    DetailAttempt,
    DetailFetchResult,
    DetailFetchStatus,
    DiscoveredJob,
    DiscoveryHit,
    DiscoveryRun,
    JobSearchPreference,
    JobSource,
    JobSourceLink,
    RawJobListing,
    SourceAttempt,
    SourceStatus,
    canonical_job_key,
    discovery_job_id,
    infer_employment_type,
    parse_daily_salary_yuan,
    parse_salary_k,
    utc_now,
)
from jobintel.models import CandidateProfile
from jobintel.persistence.repository import SQLiteJobRepository


class JobDiscoveryService:
    """Run independent sources concurrently and persist one truthful aggregate."""

    def __init__(
        self,
        repository: SQLiteJobRepository,
        connectors: dict[JobSource, JobSourceConnector],
        *,
        max_workers: int = 4,
    ) -> None:
        """Bind repository and explicit source adapters."""
        self._repository = repository
        self._connectors = connectors
        self._max_workers = max_workers

    def discover(
        self,
        preference: JobSearchPreference,
        *,
        persist: bool = True,
        detail_limit: int = 0,
        detail_cache_hours: int = 24,
    ) -> DiscoveryRun:
        """Discover, deduplicate, filter, rank, and optionally persist live jobs."""
        if not 0 <= detail_limit <= 10:
            raise ValueError("detail_limit must be between 0 and 10")
        if detail_cache_hours < 1:
            raise ValueError("detail_cache_hours must be positive")
        profile = self._repository.get_candidate_profile(
            preference.candidate_id, preference.profile_version
        )
        resolved = preference.model_copy(update={"profile_version": profile.profile_version})
        per_source_limit = (
            min(200, max(30, resolved.limit))
            if len(resolved.sources) == 1
            else min(200, max(30, math.ceil(resolved.limit / len(resolved.sources)) * 2))
        )
        listings_by_source: dict[JobSource, tuple[RawJobListing, ...]] = {}
        attempts_by_source: dict[JobSource, SourceAttempt] = {}

        with ThreadPoolExecutor(
            max_workers=min(self._max_workers, len(resolved.sources))
        ) as executor:
            futures = {}
            for source in resolved.sources:
                connector = self._connectors.get(source)
                if connector is None:
                    attempts_by_source[source] = SourceAttempt(
                        source=source,
                        status=SourceStatus.UNAVAILABLE,
                        elapsed_ms=0,
                        message="connector is not configured",
                    )
                    continue
                started = perf_counter()
                future = executor.submit(connector.search, resolved, limit=per_source_limit)
                futures[future] = (source, started)

            for future in as_completed(futures):
                source, started = futures[future]
                elapsed_ms = max(0, round((perf_counter() - started) * 1000))
                try:
                    listings = future.result()
                except AuthenticationRequiredError as exc:
                    attempts_by_source[source] = self._failed_attempt(
                        source, SourceStatus.AUTHENTICATION_REQUIRED, elapsed_ms, exc
                    )
                except SourceBlockedError as exc:
                    attempts_by_source[source] = self._failed_attempt(
                        source, SourceStatus.BLOCKED, elapsed_ms, exc
                    )
                except SourceUnavailableError as exc:
                    attempts_by_source[source] = self._failed_attempt(
                        source, SourceStatus.UNAVAILABLE, elapsed_ms, exc
                    )
                except Exception as exc:  # connector isolation is deliberate
                    attempts_by_source[source] = self._failed_attempt(
                        source, SourceStatus.FAILED, elapsed_ms, exc
                    )
                else:
                    listings_by_source[source] = listings
                    attempts_by_source[source] = SourceAttempt(
                        source=source,
                        status=SourceStatus.SUCCESS,
                        discovered_count=len(listings),
                        elapsed_ms=elapsed_ms,
                    )

        attempts = tuple(attempts_by_source[source] for source in resolved.sources)
        raw = [
            listing for source in resolved.sources for listing in listings_by_source.get(source, ())
        ]
        canonical = self._deduplicate(raw)
        eligible = [job for job in canonical if self._eligible(job, resolved)]
        hits = sorted(
            (self._rank(job, resolved, profile) for job in eligible),
            key=lambda hit: (
                -hit.rank_score,
                hit.job.company_name.casefold(),
                hit.job.title.casefold(),
                hit.job.discovery_job_id,
            ),
        )[: resolved.limit]
        enriched_hits, detail_attempts = self._enrich_details(
            hits,
            detail_limit=min(detail_limit, len(hits)),
            detail_cache_hours=detail_cache_hours,
        )
        hits = sorted(
            (self._rank(hit.job, resolved, profile) for hit in enriched_hits),
            key=lambda hit: (
                -hit.rank_score,
                hit.job.company_name.casefold(),
                hit.job.title.casefold(),
                hit.job.discovery_job_id,
            ),
        )
        run = DiscoveryRun(
            run_id=f"discovery_run_{uuid.uuid4().hex}",
            preference=resolved,
            hits=tuple(hits),
            source_attempts=attempts,
            detail_attempts=detail_attempts,
            total_discovered=len(raw),
            duplicates_removed=len(raw) - len(canonical),
            filtered_out=len(canonical) - len(eligible),
            created_at=utc_now(),
        )
        return self._repository.save_discovery_run(run) if persist else run

    def _enrich_details(
        self,
        hits: list[DiscoveryHit],
        *,
        detail_limit: int,
        detail_cache_hours: int,
    ) -> tuple[list[DiscoveryHit], tuple[DetailAttempt, ...]]:
        """Reuse fresh detail snapshots, then fetch the remaining selection conservatively."""
        if detail_limit == 0:
            return hits, ()

        selected = hits[:detail_limit]
        jobs_by_id = {hit.job.discovery_job_id: hit.job for hit in hits}
        attempts: list[DetailAttempt] = []
        pending: dict[JobSource, list[tuple[DiscoveredJob, JobSourceLink]]] = {}
        cutoff = utc_now() - timedelta(hours=detail_cache_hours)

        for hit in selected:
            job = hit.job
            link = job.source_links[0]
            cached = self._repository.find_discovered_job(job.discovery_job_id)
            if (
                cached is not None
                and cached.detail_fetched_at is not None
                and cached.detail_fetched_at >= cutoff
                and any(
                    item.source is link.source and item.external_id == link.external_id
                    for item in cached.source_links
                )
            ):
                jobs_by_id[job.discovery_job_id] = self._merge_cached_detail(job, cached)
                attempts.append(
                    DetailAttempt(
                        discovery_job_id=job.discovery_job_id,
                        source=link.source,
                        external_id=link.external_id,
                        status=DetailFetchStatus.CACHED,
                        elapsed_ms=0,
                        message=f"reused detail fetched within {detail_cache_hours}h",
                    )
                )
            else:
                pending.setdefault(link.source, []).append((job, link))

        for source, jobs_and_links in pending.items():
            connector = self._connectors.get(source)
            if connector is None or not isinstance(connector, JobDetailConnector):
                attempts.extend(
                    self._skipped_attempt(job, link, "source does not support detail enrichment")
                    for job, link in jobs_and_links
                )
                continue
            links = tuple(link for _job, link in jobs_and_links)
            try:
                results = connector.fetch_details(links)
            except Exception as exc:  # a connector-level failure must not lose search results
                status = self._detail_exception_status(exc)
                first_job, first_link = jobs_and_links[0]
                attempts.append(
                    DetailAttempt(
                        discovery_job_id=first_job.discovery_job_id,
                        source=first_link.source,
                        external_id=first_link.external_id,
                        status=status,
                        elapsed_ms=0,
                        message=(" ".join(str(exc).split())[:500] or exc.__class__.__name__),
                    )
                )
                attempts.extend(
                    self._skipped_attempt(job, link, "detail circuit breaker is open")
                    for job, link in jobs_and_links[1:]
                )
                continue
            results_by_identity = {(item.source, item.external_id): item for item in results}
            breaker_open = False
            for job, link in jobs_and_links:
                result = results_by_identity.get((link.source, link.external_id))
                if result is None:
                    attempts.append(
                        self._skipped_attempt(job, link, "detail circuit breaker is open")
                    )
                    breaker_open = True
                    continue
                attempts.append(self._detail_attempt(job, result))
                if result.detail is not None:
                    jobs_by_id[job.discovery_job_id] = job.model_copy(
                        update={
                            "description": result.detail.description,
                            "skills": result.detail.skills,
                            "company_description": result.detail.company_description,
                            "recruiter_name": result.detail.recruiter_name,
                            "recruiter_title": result.detail.recruiter_title,
                            "recruiter_active_text": result.detail.recruiter_active_text,
                            "detail_fetched_at": result.detail.fetched_at,
                            "detail_content_sha256": result.detail.content_sha256,
                        }
                    )
                elif result.status is not DetailFetchStatus.SUCCESS:
                    breaker_open = True
            if breaker_open:
                continue

        enriched = [
            hit.model_copy(update={"job": jobs_by_id[hit.job.discovery_job_id]}) for hit in hits
        ]
        return enriched, tuple(attempts)

    @staticmethod
    def _merge_cached_detail(current: DiscoveredJob, cached: DiscoveredJob) -> DiscoveredJob:
        return current.model_copy(
            update={
                "description": cached.description,
                "skills": cached.skills,
                "company_description": cached.company_description,
                "recruiter_name": cached.recruiter_name,
                "recruiter_title": cached.recruiter_title,
                "recruiter_active_text": cached.recruiter_active_text,
                "detail_fetched_at": cached.detail_fetched_at,
                "detail_content_sha256": cached.detail_content_sha256,
                "first_seen_at": cached.first_seen_at,
            }
        )

    @staticmethod
    def _detail_attempt(job: DiscoveredJob, result: DetailFetchResult) -> DetailAttempt:
        return DetailAttempt(
            discovery_job_id=job.discovery_job_id,
            source=result.source,
            external_id=result.external_id,
            status=result.status,
            elapsed_ms=result.elapsed_ms,
            message=result.message,
        )

    @staticmethod
    def _skipped_attempt(job: DiscoveredJob, link: JobSourceLink, message: str) -> DetailAttempt:
        return DetailAttempt(
            discovery_job_id=job.discovery_job_id,
            source=link.source,
            external_id=link.external_id,
            status=DetailFetchStatus.SKIPPED,
            elapsed_ms=0,
            message=message,
        )

    @staticmethod
    def _detail_exception_status(exc: Exception) -> DetailFetchStatus:
        if isinstance(exc, AuthenticationRequiredError):
            return DetailFetchStatus.AUTHENTICATION_REQUIRED
        if isinstance(exc, SourceBlockedError):
            return DetailFetchStatus.BLOCKED
        if isinstance(exc, SourceUnavailableError):
            return DetailFetchStatus.UNAVAILABLE
        return DetailFetchStatus.FAILED

    @staticmethod
    def _failed_attempt(
        source: JobSource,
        status: SourceStatus,
        elapsed_ms: int,
        exc: Exception,
    ) -> SourceAttempt:
        message = " ".join(str(exc).split())[:500] or exc.__class__.__name__
        return SourceAttempt(
            source=source,
            status=status,
            elapsed_ms=elapsed_ms,
            message=message,
        )

    @staticmethod
    def _deduplicate(listings: list[RawJobListing]) -> list[DiscoveredJob]:
        grouped: dict[str, list[RawJobListing]] = {}
        for listing in listings:
            grouped.setdefault(canonical_job_key(listing), []).append(listing)

        now = utc_now()
        jobs = []
        for key, items in grouped.items():
            richest = max(
                items,
                key=lambda item: (
                    len(item.description),
                    bool(item.salary_text),
                    bool(item.experience),
                    -list(JobSource).index(item.source),
                ),
            )
            unique_links = {
                (item.source, item.external_id): JobSourceLink(
                    source=item.source,
                    external_id=item.external_id,
                    url=item.url,
                )
                for item in items
            }
            links = tuple(
                unique_links[identity]
                for identity in sorted(
                    unique_links,
                    key=lambda identity: (list(JobSource).index(identity[0]), identity[1]),
                )
            )
            salary_min, salary_max = parse_salary_k(richest.salary_text)
            daily_salary_min, daily_salary_max = parse_daily_salary_yuan(richest.salary_text)
            employment_type = (
                richest.employment_type
                if richest.employment_type.value != "other"
                else infer_employment_type(richest.title, richest.description)
            )
            jobs.append(
                DiscoveredJob(
                    discovery_job_id=discovery_job_id(key),
                    canonical_key=key,
                    title=richest.title,
                    company_name=richest.company_name,
                    location=richest.location,
                    salary_text=richest.salary_text,
                    salary_min_k=salary_min,
                    salary_max_k=salary_max,
                    salary_daily_min_yuan=daily_salary_min,
                    salary_daily_max_yuan=daily_salary_max,
                    employment_type=employment_type,
                    company_size=next(
                        (
                            item.company_size
                            for item in items
                            if item.company_size is not CompanySize.UNKNOWN
                        ),
                        richest.company_size,
                    ),
                    description=richest.description,
                    experience=richest.experience,
                    education=richest.education,
                    published_text=richest.published_text,
                    source_links=links,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        return jobs

    @staticmethod
    def _eligible(job: DiscoveredJob, preference: JobSearchPreference) -> bool:
        text = " ".join(
            (
                job.title,
                job.company_name,
                job.description,
                job.location,
                job.education,
                job.experience,
            )
        ).casefold()
        if any(exclusion.casefold() in text for exclusion in preference.exclusions):
            return False
        if preference.city and job.location and preference.city not in job.location:
            return False
        if preference.employment_types and job.employment_type not in preference.employment_types:
            return False
        if preference.company_sizes and job.company_size not in preference.company_sizes:
            return False
        if preference.education_requirements and not any(
            value.casefold() in job.education.casefold()
            for value in preference.education_requirements
        ):
            return False
        if preference.experience_requirements and not any(
            value.casefold() in job.experience.casefold()
            for value in preference.experience_requirements
        ):
            return False
        daily_filter = (
            preference.daily_salary_min_yuan is not None
            or preference.daily_salary_max_yuan is not None
        )
        monthly_filter = preference.salary_min_k is not None or preference.salary_max_k is not None
        if daily_filter:
            if job.salary_daily_min_yuan is None or job.salary_daily_max_yuan is None:
                return preference.include_undisclosed_salary and job.salary_min_k is None
            if (
                preference.daily_salary_min_yuan is not None
                and job.salary_daily_max_yuan < preference.daily_salary_min_yuan
            ):
                return False
            return not (
                preference.daily_salary_max_yuan is not None
                and job.salary_daily_min_yuan > preference.daily_salary_max_yuan
            )
        if monthly_filter:
            if job.salary_min_k is None or job.salary_max_k is None:
                return preference.include_undisclosed_salary and job.salary_daily_min_yuan is None
            if preference.salary_min_k is not None and job.salary_max_k < preference.salary_min_k:
                return False
            return not (
                preference.salary_max_k is not None and job.salary_min_k > preference.salary_max_k
            )
        if not preference.include_undisclosed_salary:
            return job.salary_min_k is not None or job.salary_daily_min_yuan is not None
        return True

    @staticmethod
    def _rank(
        job: DiscoveredJob,
        preference: JobSearchPreference,
        profile: CandidateProfile,
    ) -> DiscoveryHit:
        title = _normalize(job.title)
        haystack = _normalize(" ".join((job.title, job.description, job.experience, job.education)))
        query_terms = _terms(preference.query)
        profile_terms = {
            normalized
            for evidence in profile.evidence
            for skill in evidence.skills
            if (normalized := _normalize(skill))
        }
        matched_query = {term for term in query_terms if term in title}
        matched_profile = {term for term in profile_terms if term in haystack}

        if _normalize(preference.query) in title:
            role_score = 50
        elif query_terms:
            role_score = round(45 * len(matched_query) / len(query_terms))
        else:
            role_score = 0
        skill_score = min(35, len(matched_profile) * 7)
        location_score = 5 if preference.city and preference.city in job.location else 0
        detail_score = 5 if len(job.description) >= 30 else 2 if job.description else 0
        salary_score = (
            5 if job.salary_min_k is not None or job.salary_daily_min_yuan is not None else 0
        )
        return DiscoveryHit(
            job=job,
            rank_score=min(
                100,
                role_score + skill_score + location_score + detail_score + salary_score,
            ),
            matched_terms=tuple(sorted(matched_query | matched_profile)),
        )


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _terms(value: str) -> set[str]:
    normalized = _normalize(value)
    terms = set(re.findall(r"[a-z0-9+#.]{2,}|[\u4e00-\u9fff]{2,}", normalized))
    for chunk in tuple(terms):
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", chunk):
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return terms
