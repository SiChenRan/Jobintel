"""Strict contracts for live job discovery, initially scoped to BOSS."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, HttpUrl, field_validator, model_validator

from jobintel.models import FrozenDomainModel, NonEmptyStr, UtcDateTime

DISCOVERY_SCHEMA_VERSION = "jobintel-discovery-v6"


class JobSource(StrEnum):
    """Maintained third-party job sources."""

    BOSS = "boss"


class DiscoveryMode(StrEnum):
    """Candidate-selected acquisition strategy within one job source."""

    SEARCH = "search"
    RECOMMENDATION = "recommendation"
    HYBRID = "hybrid"


class DiscoveryChannel(StrEnum):
    """Traceable surface on which a source exposed a listing."""

    SEARCH = "search"
    RECOMMENDATION = "recommendation"


class SourceStatus(StrEnum):
    """Truthful outcome of one source attempt."""

    SUCCESS = "success"
    AUTHENTICATION_REQUIRED = "authentication_required"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class DetailFetchStatus(StrEnum):
    """Outcome of one requested job-detail enrichment."""

    SUCCESS = "success"
    CACHED = "cached"
    AUTHENTICATION_REQUIRED = "authentication_required"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    SKIPPED = "skipped"


class EmploymentType(StrEnum):
    """Normalized employment arrangement used by deterministic filters."""

    FULL_TIME = "full_time"
    INTERNSHIP = "internship"
    PART_TIME = "part_time"
    OTHER = "other"


class CompanySize(StrEnum):
    """Normalized company headcount bands exposed by job sources."""

    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    VERY_LARGE = "very_large"
    ENTERPRISE = "enterprise"
    UNKNOWN = "unknown"


class RadarEventStatus(StrEnum):
    """Change classification between two successful discovery snapshots."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    CLOSED = "closed"


class JobSearchPreference(FrozenDomainModel):
    """One bounded live search requested by a candidate."""

    candidate_id: NonEmptyStr
    profile_version: int | None = Field(default=None, ge=1)
    query: NonEmptyStr = Field(max_length=100)
    city: str = Field(default="", max_length=50)
    salary_min_k: int | None = Field(default=None, ge=0, le=1000)
    salary_max_k: int | None = Field(default=None, ge=0, le=1000)
    daily_salary_min_yuan: int | None = Field(default=None, ge=0, le=10000)
    daily_salary_max_yuan: int | None = Field(default=None, ge=0, le=10000)
    include_undisclosed_salary: bool = True
    employment_types: tuple[EmploymentType, ...] = ()
    company_sizes: tuple[CompanySize, ...] = ()
    education_requirements: tuple[NonEmptyStr, ...] = ()
    experience_requirements: tuple[NonEmptyStr, ...] = ()
    exclusions: tuple[NonEmptyStr, ...] = ()
    smart_expand: bool = False
    expanded_queries: tuple[NonEmptyStr, ...] = Field(default=(), max_length=2)
    discovery_mode: DiscoveryMode = DiscoveryMode.SEARCH
    prefer_new: bool = True
    only_new: bool = False
    sources: tuple[JobSource, ...] = (JobSource.BOSS,)
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, sources: tuple[JobSource, ...]) -> tuple[JobSource, ...]:
        """Require at least one unique source."""
        if not sources:
            raise ValueError("at least one discovery source is required")
        if len(sources) != len(set(sources)):
            raise ValueError("discovery sources must be unique")
        return sources

    @field_validator("employment_types")
    @classmethod
    def unique_employment_types(
        cls, values: tuple[EmploymentType, ...]
    ) -> tuple[EmploymentType, ...]:
        """Reject repeated employment-type filters."""
        if len(values) != len(set(values)):
            raise ValueError("employment types must be unique")
        return values

    @field_validator("company_sizes")
    @classmethod
    def unique_company_sizes(cls, values: tuple[CompanySize, ...]) -> tuple[CompanySize, ...]:
        """Reject repeated company-size filters and the unknown sentinel."""
        if len(values) != len(set(values)):
            raise ValueError("company sizes must be unique")
        if CompanySize.UNKNOWN in values:
            raise ValueError("unknown company size cannot be used as a filter")
        return values

    @field_validator("expanded_queries")
    @classmethod
    def unique_expanded_queries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep program-generated supplemental searches bounded and unique."""
        normalized = tuple(_canonical_text(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("expanded queries must be unique")
        return values

    @model_validator(mode="after")
    def valid_salary_range(self) -> JobSearchPreference:
        """Reject inverted or mixed-unit salary constraints."""
        if (
            self.salary_min_k is not None
            and self.salary_max_k is not None
            and self.salary_min_k > self.salary_max_k
        ):
            raise ValueError("salary_min_k cannot exceed salary_max_k")
        if (
            self.daily_salary_min_yuan is not None
            and self.daily_salary_max_yuan is not None
            and self.daily_salary_min_yuan > self.daily_salary_max_yuan
        ):
            raise ValueError("daily_salary_min_yuan cannot exceed daily_salary_max_yuan")
        monthly = self.salary_min_k is not None or self.salary_max_k is not None
        daily = self.daily_salary_min_yuan is not None or self.daily_salary_max_yuan is not None
        if monthly and daily:
            raise ValueError("monthly and daily salary filters cannot be mixed")
        if self.expanded_queries and not self.smart_expand:
            raise ValueError("expanded queries require smart expansion")
        if _canonical_text(self.query) in {
            _canonical_text(value) for value in self.expanded_queries
        }:
            raise ValueError("expanded queries must differ from the primary query")
        return self


class RawJobListing(FrozenDomainModel):
    """One untrusted listing normalized at a connector boundary."""

    source: JobSource
    external_id: NonEmptyStr
    title: NonEmptyStr
    company_name: NonEmptyStr
    location: str = ""
    salary_text: str = ""
    description: str = ""
    experience: str = ""
    education: str = ""
    employment_type: EmploymentType = EmploymentType.OTHER
    company_size: CompanySize = CompanySize.UNKNOWN
    url: HttpUrl
    published_text: str = ""
    acquisition_channels: tuple[DiscoveryChannel, ...] = Field(
        default=(DiscoveryChannel.SEARCH,), min_length=1
    )

    @field_validator("acquisition_channels")
    @classmethod
    def unique_acquisition_channels(
        cls, values: tuple[DiscoveryChannel, ...]
    ) -> tuple[DiscoveryChannel, ...]:
        """Keep a listing's discovery surfaces non-empty and unique."""
        if len(values) != len(set(values)):
            raise ValueError("acquisition channels must be unique")
        return values


class JobSourceLink(FrozenDomainModel):
    """Traceable platform identity for a canonical discovered job."""

    source: JobSource
    external_id: NonEmptyStr
    url: HttpUrl


class RawJobDetail(FrozenDomainModel):
    """One full job-detail snapshot returned by a source connector."""

    source: JobSource
    external_id: NonEmptyStr
    url: HttpUrl
    description: NonEmptyStr
    skills: tuple[NonEmptyStr, ...] = ()
    company_description: str = ""
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_active_text: str = ""
    fetched_at: UtcDateTime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("skills")
    @classmethod
    def unique_skills(cls, skills: tuple[NonEmptyStr, ...]) -> tuple[NonEmptyStr, ...]:
        """Keep skill labels stable without repeated page decorations."""
        return tuple(dict.fromkeys(skills))


class DetailFetchResult(FrozenDomainModel):
    """Connector-level result for one detail URL, including honest failures."""

    source: JobSource
    external_id: NonEmptyStr
    status: DetailFetchStatus
    elapsed_ms: int = Field(ge=0)
    detail: RawJobDetail | None = None
    message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def coherent_result(self) -> DetailFetchResult:
        """Require content only for successful fetches."""
        if (self.status is DetailFetchStatus.SUCCESS) != (self.detail is not None):
            raise ValueError("successful detail fetch must contain detail and failures must not")
        return self


class DiscoveredJob(FrozenDomainModel):
    """Cross-platform canonical job assembled from one or more listings."""

    discovery_job_id: NonEmptyStr
    canonical_key: NonEmptyStr
    title: NonEmptyStr
    company_name: NonEmptyStr
    location: str = ""
    salary_text: str = ""
    salary_min_k: int | None = Field(default=None, ge=0)
    salary_max_k: int | None = Field(default=None, ge=0)
    salary_daily_min_yuan: int | None = Field(default=None, ge=0)
    salary_daily_max_yuan: int | None = Field(default=None, ge=0)
    employment_type: EmploymentType = EmploymentType.OTHER
    company_size: CompanySize = CompanySize.UNKNOWN
    description: str = ""
    experience: str = ""
    education: str = ""
    published_text: str = ""
    skills: tuple[NonEmptyStr, ...] = ()
    company_description: str = ""
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_active_text: str = ""
    detail_fetched_at: UtcDateTime | None = None
    detail_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_links: tuple[JobSourceLink, ...] = Field(min_length=1)
    acquisition_channels: tuple[DiscoveryChannel, ...] = Field(
        default=(DiscoveryChannel.SEARCH,), min_length=1
    )
    first_seen_at: UtcDateTime
    last_seen_at: UtcDateTime

    @field_validator("source_links")
    @classmethod
    def unique_source_links(cls, links: tuple[JobSourceLink, ...]) -> tuple[JobSourceLink, ...]:
        """Reject repeated source identities."""
        identities = [(link.source, link.external_id) for link in links]
        if len(identities) != len(set(identities)):
            raise ValueError("source links must be unique")
        return links

    @field_validator("acquisition_channels")
    @classmethod
    def unique_job_acquisition_channels(
        cls, values: tuple[DiscoveryChannel, ...]
    ) -> tuple[DiscoveryChannel, ...]:
        """Keep all surfaces contributing to a canonical job traceable."""
        if len(values) != len(set(values)):
            raise ValueError("acquisition channels must be unique")
        return values

    @field_validator("skills")
    @classmethod
    def unique_detail_skills(cls, skills: tuple[NonEmptyStr, ...]) -> tuple[NonEmptyStr, ...]:
        """Reject duplicated normalized detail labels."""
        if len(skills) != len(set(skills)):
            raise ValueError("detail skills must be unique")
        return skills

    @model_validator(mode="after")
    def coherent_detail_metadata(self) -> DiscoveredJob:
        """Keep detail time and content identity atomic."""
        if (self.detail_fetched_at is None) != (self.detail_content_sha256 is None):
            raise ValueError("detail timestamp and content digest must be set together")
        return self


class SearchProfileSnapshot(FrozenDomainModel):
    """Compact immutable profile context used by one discovery run."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    evidence_count: int = Field(ge=0)
    skill_count: int = Field(default=0, ge=0)
    skills: tuple[NonEmptyStr, ...] = ()
    expansion_queries: tuple[NonEmptyStr, ...] = Field(default=(), max_length=2)

    @field_validator("skills", "expansion_queries")
    @classmethod
    def unique_snapshot_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep displayed profile context deterministic and duplicate-free."""
        normalized = tuple(_canonical_text(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("profile snapshot values must be unique")
        return values


class ProfileEvidenceMatch(FrozenDomainModel):
    """One candidate evidence item that contributed to discovery ranking."""

    evidence_id: NonEmptyStr
    title: NonEmptyStr
    matched_terms: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("matched_terms")
    @classmethod
    def unique_matched_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate terms within one evidence explanation."""
        normalized = tuple(_canonical_text(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("matched evidence terms must be unique")
        return values


class DiscoveryRankBreakdown(FrozenDomainModel):
    """Explainable deterministic components of one coarse discovery score."""

    target_relevance: int = Field(ge=0, le=40)
    profile_skills: int = Field(ge=0, le=30)
    profile_evidence: int = Field(ge=0, le=10)
    preference_fit: int = Field(ge=0, le=10)
    information_quality: int = Field(ge=0, le=10)
    total: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def total_matches_components(self) -> DiscoveryRankBreakdown:
        """Prevent displayed score explanations from drifting from their total."""
        expected = (
            self.target_relevance
            + self.profile_skills
            + self.profile_evidence
            + self.preference_fit
            + self.information_quality
        )
        if self.total != expected:
            raise ValueError("discovery rank total does not match its components")
        return self


class DiscoveryHit(FrozenDomainModel):
    """A hard-filtered job with deterministic and explainable ranking evidence."""

    job: DiscoveredJob
    rank_score: int = Field(ge=0, le=100)
    matched_terms: tuple[NonEmptyStr, ...] = ()
    matched_query_terms: tuple[NonEmptyStr, ...] = ()
    matched_profile_skills: tuple[NonEmptyStr, ...] = ()
    matched_evidence: tuple[ProfileEvidenceMatch, ...] = ()
    rank_breakdown: DiscoveryRankBreakdown | None = None
    is_new_to_candidate: bool = True

    @model_validator(mode="after")
    def coherent_ranking_explanation(self) -> DiscoveryHit:
        """Keep the legacy score and the explainable score in agreement."""
        if self.rank_breakdown is not None and self.rank_breakdown.total != self.rank_score:
            raise ValueError("rank score does not match discovery rank breakdown")
        if len(self.matched_profile_skills) != len(set(self.matched_profile_skills)):
            raise ValueError("matched profile skills must be unique")
        evidence_ids = tuple(item.evidence_id for item in self.matched_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("matched profile evidence must be unique")
        return self


class SourceAttempt(FrozenDomainModel):
    """Observable result from one live source, including honest failures."""

    source: JobSource
    status: SourceStatus
    discovered_count: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(ge=0)
    message: str | None = Field(default=None, max_length=500)


class DetailAttempt(FrozenDomainModel):
    """Observable outcome of enriching one ranked job."""

    discovery_job_id: NonEmptyStr
    source: JobSource
    external_id: NonEmptyStr
    status: DetailFetchStatus
    elapsed_ms: int = Field(ge=0)
    message: str | None = Field(default=None, max_length=500)


class DiscoveryRun(FrozenDomainModel):
    """Complete persisted output of one live search."""

    run_id: NonEmptyStr
    preference: JobSearchPreference
    hits: tuple[DiscoveryHit, ...]
    source_attempts: tuple[SourceAttempt, ...]
    detail_attempts: tuple[DetailAttempt, ...] = ()
    total_discovered: int = Field(ge=0)
    duplicates_removed: int = Field(ge=0)
    filtered_out: int = Field(ge=0)
    profile_snapshot: SearchProfileSnapshot | None = None
    created_at: UtcDateTime
    schema_version: NonEmptyStr = DISCOVERY_SCHEMA_VERSION

    @model_validator(mode="after")
    def validate_counts(self) -> DiscoveryRun:
        """Keep reported discovery counts internally consistent."""
        if self.total_discovered != sum(item.discovered_count for item in self.source_attempts):
            raise ValueError("total_discovered does not match source attempts")
        if len(self.source_attempts) != len(self.preference.sources):
            raise ValueError("source attempt count does not match requested sources")
        if self.profile_snapshot is not None and (
            self.profile_snapshot.candidate_id != self.preference.candidate_id
            or self.profile_snapshot.profile_version != self.preference.profile_version
        ):
            raise ValueError("profile snapshot does not match discovery preference")
        hit_ids = {hit.job.discovery_job_id for hit in self.hits}
        detail_keys = [
            (attempt.discovery_job_id, attempt.source, attempt.external_id)
            for attempt in self.detail_attempts
        ]
        if len(detail_keys) != len(set(detail_keys)):
            raise ValueError("detail attempts must be unique")
        if any(attempt.discovery_job_id not in hit_ids for attempt in self.detail_attempts):
            raise ValueError("detail attempt references a job outside this run")
        return self


class RadarEvent(FrozenDomainModel):
    """One immutable job-level difference in a radar check."""

    discovery_job_id: NonEmptyStr
    status: RadarEventStatus
    job: DiscoveredJob
    previous_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    current_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent_hashes(self) -> RadarEvent:
        """Require digest presence consistent with the event direction."""
        if self.status is RadarEventStatus.NEW and self.previous_content_sha256 is not None:
            raise ValueError("new radar events cannot have a previous digest")
        if self.status is RadarEventStatus.CLOSED and self.current_content_sha256 is not None:
            raise ValueError("closed radar events cannot have a current digest")
        if self.status is not RadarEventStatus.NEW and self.previous_content_sha256 is None:
            raise ValueError("non-new radar events require a previous digest")
        if self.status is not RadarEventStatus.CLOSED and self.current_content_sha256 is None:
            raise ValueError("non-closed radar events require a current digest")
        return self


class RadarCheck(FrozenDomainModel):
    """Persisted comparison between a baseline and a successful current run."""

    run_id: NonEmptyStr
    baseline_run_id: NonEmptyStr
    preference_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: tuple[RadarEvent, ...]
    created_at: UtcDateTime

    @model_validator(mode="after")
    def unique_jobs(self) -> RadarCheck:
        """Classify every canonical job at most once."""
        identities = [event.discovery_job_id for event in self.events]
        if len(identities) != len(set(identities)):
            raise ValueError("radar event jobs must be unique")
        return self


def canonical_job_key(listing: RawJobListing) -> str:
    """Build a stable cross-platform deduplication key."""
    parts = (
        listing.company_name,
        listing.title,
        _coarse_location(listing.location),
    )
    return "|".join(_canonical_text(part) for part in parts)


def discovery_job_id(canonical_key: str) -> str:
    """Build a stable namespaced job identity from a canonical key."""
    digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
    return f"discovery_{digest[:24]}"


def content_digest(value: object) -> str:
    """Hash a JSON-compatible value for idempotent persistence."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def discovered_job_content_sha256(job: DiscoveredJob) -> str:
    """Hash stable search-card fields without volatile activity or detail-cache data."""
    return content_digest(
        {
            "discovery_job_id": job.discovery_job_id,
            "title": job.title,
            "company_name": job.company_name,
            "location": job.location,
            "salary_text": job.salary_text,
            "salary_min_k": job.salary_min_k,
            "salary_max_k": job.salary_max_k,
            "salary_daily_min_yuan": job.salary_daily_min_yuan,
            "salary_daily_max_yuan": job.salary_daily_max_yuan,
            "employment_type": job.employment_type,
            "experience": job.experience,
            "education": job.education,
            "source_links": [link.model_dump(mode="json") for link in job.source_links],
        }
    )


def preference_fingerprint(preference: JobSearchPreference) -> str:
    """Identify an exact candidate search configuration for cooldown tracking."""
    return content_digest(preference.model_dump(mode="json"))


def parse_salary_k(text: str) -> tuple[int | None, int | None]:
    """Parse common Chinese monthly salary labels into integer K bounds."""
    normalized = unicodedata.normalize("NFKC", text).replace(" ", "").casefold()
    match = re.search(r"(\d+(?:\.\d+)?)[-~至](\d+(?:\.\d+)?)(k|千)", normalized)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        return round(low), round(high)
    yuan = re.search(r"(\d+)[-~至](\d+)元/月", normalized)
    if yuan:
        return round(int(yuan.group(1)) / 1000), round(int(yuan.group(2)) / 1000)
    annual = re.search(r"(\d+(?:\.\d+)?)[-~至](\d+(?:\.\d+)?)万/年", normalized)
    if annual:
        return round(float(annual.group(1)) * 10 / 12), round(float(annual.group(2)) * 10 / 12)
    return None, None


def parse_daily_salary_yuan(text: str) -> tuple[int | None, int | None]:
    """Parse common Chinese daily salary labels into integer yuan bounds."""
    normalized = unicodedata.normalize("NFKC", text).replace(" ", "").casefold()
    ranged = re.search(r"(\d+)[-~至](\d+)元/(?:天|日)", normalized)
    if ranged:
        return int(ranged.group(1)), int(ranged.group(2))
    exact = re.search(r"(\d+)元/(?:天|日)", normalized)
    if exact:
        value = int(exact.group(1))
        return value, value
    return None, None


def infer_employment_type(*values: str) -> EmploymentType:
    """Infer a conservative employment type from source labels and listing text."""
    normalized = _canonical_text(" ".join(values))
    if "实习" in normalized or "intern" in normalized:
        return EmploymentType.INTERNSHIP
    if "兼职" in normalized or "parttime" in normalized:
        return EmploymentType.PART_TIME
    if "全职" in normalized or "fulltime" in normalized:
        return EmploymentType.FULL_TIME
    return EmploymentType.OTHER


def parse_company_size(value: str) -> CompanySize:
    """Normalize BOSS-style headcount labels without guessing absent values."""
    normalized = _canonical_text(value).replace("人", "")
    mapping = {
        "0-20": CompanySize.MICRO,
        "20-99": CompanySize.SMALL,
        "100-499": CompanySize.MEDIUM,
        "500-999": CompanySize.LARGE,
        "1000-9999": CompanySize.VERY_LARGE,
        "10000以上": CompanySize.ENTERPRISE,
        "10000+": CompanySize.ENTERPRISE,
    }
    return mapping.get(normalized, CompanySize.UNKNOWN)


def utc_now() -> datetime:
    """Return an aware UTC timestamp through one testable seam."""
    return datetime.now(UTC)


def _canonical_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _coarse_location(value: str) -> str:
    return re.split(r"[·・/,-]", value, maxsplit=1)[0]
