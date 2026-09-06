from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from jobintel.discovery.connectors.base import (
    AuthenticationRequiredError,
)
from jobintel.discovery.models import (
    CompanySize,
    DetailFetchResult,
    DetailFetchStatus,
    DiscoveryChannel,
    DiscoveryMode,
    DiscoveryRun,
    EmploymentType,
    JobSearchPreference,
    JobSource,
    JobSourceLink,
    RadarEventStatus,
    RawJobDetail,
    RawJobListing,
    SourceStatus,
    canonical_job_key,
    parse_company_size,
    parse_daily_salary_yuan,
    parse_salary_k,
)
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import RadarCooldownError
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.services.radar import JobRadarService


def _listing(
    source: JobSource,
    external_id: str,
    *,
    title: str = "Python 后端工程师",
    company: str = "星云科技",
    location: str = "上海 · 浦东",
    salary: str = "25-40K",
    description: str = "Python FastAPI PostgreSQL Kubernetes",
    company_size: CompanySize = CompanySize.UNKNOWN,
) -> RawJobListing:
    return RawJobListing(
        source=source,
        external_id=external_id,
        title=title,
        company_name=company,
        location=location,
        salary_text=salary,
        description=description,
        experience="3-5年",
        education="本科",
        company_size=company_size,
        url=f"https://example.com/{source.value}/{external_id}",
        published_text="今天",
    )


@dataclass
class FakeConnector:
    source: JobSource
    listings: tuple[RawJobListing, ...] = ()
    error: Exception | None = None

    def search(self, preference: JobSearchPreference, *, limit: int) -> tuple[RawJobListing, ...]:
        assert preference.profile_version == 2
        if self.error is not None:
            raise self.error
        return self.listings[:limit]


@dataclass
class FakeDetailConnector(FakeConnector):
    fetch_calls: int = 0

    def fetch_details(self, links: tuple[JobSourceLink, ...]) -> tuple[DetailFetchResult, ...]:
        self.fetch_calls += 1
        return tuple(
            DetailFetchResult(
                source=link.source,
                external_id=link.external_id,
                status=DetailFetchStatus.SUCCESS,
                elapsed_ms=12,
                detail=RawJobDetail(
                    source=link.source,
                    external_id=link.external_id,
                    url=link.url,
                    description=(
                        "完整职位描述: 负责 Python、FastAPI、PostgreSQL、Redis 与 Kubernetes "
                        "生产平台研发和稳定性建设。"
                    ),
                    skills=("Python", "FastAPI", "PostgreSQL"),
                    company_description="真实公司介绍。",
                    recruiter_name="沈先生",
                    recruiter_title="研发负责人",
                    recruiter_active_text="刚刚活跃",
                    fetched_at=datetime.now(UTC),
                    content_sha256="a" * 64,
                ),
            )
            for link in links
        )


@dataclass
class BlockingDetailConnector(FakeConnector):
    def fetch_details(self, links: tuple[JobSourceLink, ...]) -> tuple[DetailFetchResult, ...]:
        first = links[0]
        return (
            DetailFetchResult(
                source=first.source,
                external_id=first.external_id,
                status=DetailFetchStatus.BLOCKED,
                elapsed_ms=7,
                message="security verification",
            ),
        )


def test_salary_parser_supports_common_monthly_and_annual_labels() -> None:
    assert parse_salary_k("25-40K·14薪") == (25, 40)
    assert parse_salary_k("20000-35000元/月") == (20, 35)
    assert parse_salary_k("24-36万/年") == (20, 30)
    assert parse_salary_k("面议") == (None, None)
    assert parse_daily_salary_yuan("200-350元/天") == (200, 350)
    assert parse_daily_salary_yuan("300元/日") == (300, 300)
    assert parse_daily_salary_yuan("25-40K") == (None, None)


def test_company_size_parser_supports_boss_headcount_labels() -> None:
    assert parse_company_size("20-99人") is CompanySize.SMALL
    assert parse_company_size("10000人以上") is CompanySize.ENTERPRISE
    assert parse_company_size("") is CompanySize.UNKNOWN


def test_company_size_filter_excludes_other_and_undisclosed_sizes(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                (
                    _listing(
                        JobSource.BOSS,
                        "small",
                        company="小型团队",
                        company_size=CompanySize.SMALL,
                    ),
                    _listing(
                        JobSource.BOSS,
                        "enterprise",
                        company="大型公司",
                        company_size=CompanySize.ENTERPRISE,
                    ),
                    _listing(JobSource.BOSS, "unknown", company="规模未披露公司"),
                ),
            )
        },
    )

    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            company_sizes=(CompanySize.SMALL,),
            sources=(JobSource.BOSS,),
        ),
        persist=False,
    )

    assert [hit.job.company_name for hit in run.hits] == ["小型团队"]
    assert run.hits[0].job.company_size is CompanySize.SMALL
    assert run.filtered_out == 2


def test_discovery_marks_candidate_history_and_can_return_only_new_jobs(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    old = _listing(
        JobSource.BOSS,
        "old",
        title="Python 后端实习",
        company="历史公司",
    )
    new = _listing(
        JobSource.BOSS,
        "new",
        title="Agent 开发实习",
        company="新公司",
        description="Python LangChain Agent",
    ).model_copy(update={"acquisition_channels": (DiscoveryChannel.RECOMMENDATION,)})
    preference = JobSearchPreference(
        candidate_id="C001",
        query="Agent Python",
        discovery_mode=DiscoveryMode.HYBRID,
        limit=10,
    )

    first = JobDiscoveryService(
        jobintel_repo,
        {JobSource.BOSS: FakeConnector(JobSource.BOSS, (old,))},
    ).discover(preference)
    assert first.hits[0].is_new_to_candidate is True

    second = JobDiscoveryService(
        jobintel_repo,
        {JobSource.BOSS: FakeConnector(JobSource.BOSS, (old, new))},
    ).discover(preference, persist=False)
    assert [hit.job.company_name for hit in second.hits] == ["新公司", "历史公司"]
    assert [hit.is_new_to_candidate for hit in second.hits] == [True, False]
    assert second.hits[0].job.acquisition_channels == (DiscoveryChannel.RECOMMENDATION,)

    only_new = JobDiscoveryService(
        jobintel_repo,
        {JobSource.BOSS: FakeConnector(JobSource.BOSS, (old, new))},
    ).discover(preference.model_copy(update={"only_new": True}), persist=False)
    assert [hit.job.company_name for hit in only_new.hits] == ["新公司"]
    assert only_new.filtered_out == 1


def test_daily_salary_and_internship_filters_use_correct_units(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                (
                    _listing(
                        JobSource.BOSS,
                        "intern-low",
                        title="Agent 开发实习",
                        company="低日薪公司",
                        salary="120-150元/天",
                    ),
                    _listing(
                        JobSource.BOSS,
                        "intern-fit",
                        title="Agent 开发实习",
                        company="合适日薪公司",
                        salary="200-300元/天",
                    ),
                    _listing(
                        JobSource.BOSS,
                        "full-time",
                        title="Agent 开发工程师",
                        salary="25-40K",
                    ),
                ),
            )
        },
    )

    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Agent 开发实习",
            daily_salary_min_yuan=200,
            employment_types=(EmploymentType.INTERNSHIP,),
            sources=(JobSource.BOSS,),
        ),
        persist=False,
    )

    assert [hit.job.salary_text for hit in run.hits] == ["200-300元/天"]
    assert run.hits[0].job.salary_daily_min_yuan == 200
    assert run.hits[0].job.employment_type is EmploymentType.INTERNSHIP


def test_education_experience_and_exclusion_preset_fields_filter_jobs(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                (
                    _listing(JobSource.BOSS, "match"),
                    _listing(
                        JobSource.BOSS,
                        "excluded",
                        title="Python 外包工程师",
                        company="驻场供应商",
                    ),
                ),
            )
        },
    )
    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            education_requirements=("本科",),
            experience_requirements=("3-5年",),
            exclusions=("驻场",),
        ),
        persist=False,
    )

    assert [hit.job.source_links[0].external_id for hit in run.hits] == ["match"]


def test_canonical_key_normalizes_cross_platform_identity() -> None:
    first = _listing(JobSource.BOSS, "b1")
    second = _listing(
        JobSource.BOSS,
        "l1",
        title=" Python 后端工程师 ",
        company="星云科技",
        location="上海-徐汇",
    )
    assert canonical_job_key(first) == canonical_job_key(second)


def test_discovery_deduplicates_filters_ranks_and_persists(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    duplicate = _listing(
        JobSource.BOSS,
        "l1",
        description="Python FastAPI PostgreSQL Kubernetes MLOps production platform",
        company_size=CompanySize.SMALL,
    )
    low_salary = _listing(
        JobSource.BOSS,
        "l2",
        title="Python 初级工程师",
        company="低薪公司",
        salary="8-12K",
    )
    excluded = _listing(
        JobSource.BOSS,
        "b2",
        title="Python 外包工程师",
        company="外包供应商",
    )
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                (
                    _listing(JobSource.BOSS, "b1"),
                    _listing(JobSource.BOSS, "b1"),
                    duplicate,
                    low_salary,
                    excluded,
                ),
            ),
        },
    )
    preference = JobSearchPreference(
        candidate_id="C001",
        query="Python 后端",
        city="上海",
        salary_min_k=20,
        exclusions=("外包",),
        sources=(JobSource.BOSS,),
        limit=10,
    )

    run = service.discover(preference)

    assert run.preference.profile_version == 2
    assert run.total_discovered == 5
    assert run.duplicates_removed == 2
    assert run.filtered_out == 2
    assert len(run.hits) == 1
    assert len(run.hits[0].job.source_links) == 2
    assert run.hits[0].job.description == duplicate.description
    assert run.hits[0].job.company_size is CompanySize.SMALL
    assert "python" in run.hits[0].matched_terms
    assert run.hits[0].rank_score > 50
    restored = jobintel_repo.get_discovery_run(run.run_id)
    assert restored == run
    assert restored.hits[0].job.company_size is CompanySize.SMALL


def test_discovery_reports_source_failure_and_dry_run_does_not_persist(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                error=AuthenticationRequiredError("login"),
            ),
        },
    )

    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            sources=(JobSource.BOSS,),
            limit=5,
        ),
        persist=False,
    )

    assert [item.status for item in run.source_attempts] == [
        SourceStatus.AUTHENTICATION_REQUIRED,
    ]
    assert not run.hits
    with pytest.raises(Exception, match="discovery run not found"):
        jobintel_repo.get_discovery_run(run.run_id)


def test_strict_salary_removes_undisclosed_jobs(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {
            JobSource.BOSS: FakeConnector(
                JobSource.BOSS,
                (_listing(JobSource.BOSS, "b1", salary="面议"),),
            )
        },
    )
    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            include_undisclosed_salary=False,
            sources=(JobSource.BOSS,),
        ),
        persist=False,
    )
    assert not run.hits
    assert run.filtered_out == 1


def test_discovery_request_rejects_invalid_sources_and_salary() -> None:
    with pytest.raises(ValueError, match="unique"):
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            sources=(JobSource.BOSS, JobSource.BOSS),
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            salary_min_k=40,
            salary_max_k=20,
        )
    with pytest.raises(ValueError, match="cannot be mixed"):
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            salary_min_k=20,
            daily_salary_min_yuan=200,
        )
    with pytest.raises(ValueError, match="require smart expansion"):
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            expanded_queries=("后端开发",),
        )
    with pytest.raises(ValueError, match="must differ"):
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            smart_expand=True,
            expanded_queries=("python",),
        )


def test_discovery_run_validates_source_count_totals(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    service = JobDiscoveryService(
        jobintel_repo,
        {JobSource.BOSS: FakeConnector(JobSource.BOSS)},
    )
    valid = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Python",
            sources=(JobSource.BOSS,),
        ),
        persist=False,
    )
    with pytest.raises(ValueError, match="total_discovered"):
        DiscoveryRun.model_validate({**valid.model_dump(), "total_discovered": 1})


def test_detail_enrichment_is_persisted_and_reused_from_cache(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    connector = FakeDetailConnector(
        JobSource.BOSS,
        (_listing(JobSource.BOSS, "b1", description="Python"),),
    )
    service = JobDiscoveryService(jobintel_repo, {JobSource.BOSS: connector})
    preference = JobSearchPreference(
        candidate_id="C001",
        query="Python 后端",
        sources=(JobSource.BOSS,),
        limit=5,
    )

    first = service.discover(preference, detail_limit=1)

    assert first.detail_attempts[0].status is DetailFetchStatus.SUCCESS
    assert first.hits[0].job.description.startswith("完整职位描述")
    assert first.hits[0].job.recruiter_name == "沈先生"
    assert jobintel_repo.get_discovery_run(first.run_id) == first

    second = service.discover(preference, detail_limit=1)

    assert second.detail_attempts[0].status is DetailFetchStatus.CACHED
    assert second.hits[0].job.description == first.hits[0].job.description
    assert connector.fetch_calls == 1
    assert jobintel_repo.get_discovery_run(second.run_id) == second


def test_detail_enrichment_marks_remaining_jobs_skipped_after_risk_control(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    connector = BlockingDetailConnector(
        JobSource.BOSS,
        (
            _listing(JobSource.BOSS, "b1", company="甲公司"),
            _listing(JobSource.BOSS, "b2", company="乙公司"),
        ),
    )
    service = JobDiscoveryService(jobintel_repo, {JobSource.BOSS: connector})

    run = service.discover(
        JobSearchPreference(candidate_id="C001", query="Python", limit=5),
        detail_limit=2,
        persist=False,
    )

    assert [attempt.status for attempt in run.detail_attempts] == [
        DetailFetchStatus.BLOCKED,
        DetailFetchStatus.SKIPPED,
    ]


def test_radar_classifies_new_changed_closed_and_preserves_snapshots(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    connector = FakeConnector(
        JobSource.BOSS,
        (
            _listing(JobSource.BOSS, "kept", company="保留公司", salary="20-30K"),
            _listing(JobSource.BOSS, "closed", company="下线公司", salary="20-30K"),
        ),
    )
    discovery = JobDiscoveryService(jobintel_repo, {JobSource.BOSS: connector})
    baseline = discovery.discover(
        JobSearchPreference(candidate_id="C001", query="Python", limit=10)
    )
    connector.listings = (
        _listing(JobSource.BOSS, "kept", company="保留公司", salary="25-35K"),
        _listing(JobSource.BOSS, "new", company="新增公司", salary="22-32K"),
    )

    check = JobRadarService(jobintel_repo, discovery).check(baseline.run_id, force=True)

    assert {event.status for event in check.events} == {
        RadarEventStatus.NEW,
        RadarEventStatus.CHANGED,
        RadarEventStatus.CLOSED,
    }
    assert jobintel_repo.get_radar_check(check.run_id) == check
    reloaded_baseline = jobintel_repo.get_discovery_run(baseline.run_id)
    kept = next(hit.job for hit in reloaded_baseline.hits if hit.job.company_name == "保留公司")
    assert kept.salary_text == "20-30K"

    with pytest.raises(RadarCooldownError, match="cooldown"):
        JobRadarService(jobintel_repo, discovery).check(check.run_id)
    with pytest.raises(ValueError, match="stale"):
        JobRadarService(jobintel_repo, discovery).check(baseline.run_id, force=True)
