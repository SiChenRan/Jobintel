from __future__ import annotations

from dataclasses import dataclass, field

from jobintel.discovery.models import JobSearchPreference, JobSource, RawJobListing
from jobintel.discovery.service import JobDiscoveryService
from jobintel.persistence.repository import SQLiteJobRepository


def _listing(external_id: str, *, company: str, description: str) -> RawJobListing:
    return RawJobListing(
        source=JobSource.BOSS,
        external_id=external_id,
        title="平台工程岗位",
        company_name=company,
        location="北京",
        salary_text="200-300元/天",
        description=description,
        experience="经验不限",
        education="本科",
        url=f"https://example.com/jobs/{external_id}",
        published_text="今天",
    )


@dataclass
class _Connector:
    source: JobSource = JobSource.BOSS
    preferences: list[JobSearchPreference] = field(default_factory=list)

    def search(self, preference: JobSearchPreference, *, limit: int) -> tuple[RawJobListing, ...]:
        self.preferences.append(preference)
        return (
            _listing(
                "backend",
                company="后端平台公司",
                description="Python FastAPI PostgreSQL Kubernetes 平台服务开发",
            ),
            _listing(
                "program",
                company="项目管理公司",
                description="Program management Cloud migration English 跨团队项目管理",
            ),
        )[:limit]


def test_different_candidate_profiles_produce_different_explainable_rankings(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    connector = _Connector()
    service = JobDiscoveryService(jobintel_repo, {JobSource.BOSS: connector})

    backend_run = service.discover(
        JobSearchPreference(candidate_id="C001", query="平台", limit=2),
        persist=False,
    )
    program_run = service.discover(
        JobSearchPreference(candidate_id="C002", query="平台", limit=2),
        persist=False,
    )

    assert backend_run.hits[0].job.company_name == "后端平台公司"
    assert program_run.hits[0].job.company_name == "项目管理公司"
    assert backend_run.profile_snapshot is not None
    assert backend_run.profile_snapshot.profile_version == 2
    assert backend_run.profile_snapshot.evidence_count == 5
    assert {"Python", "FastAPI", "PostgreSQL"} <= set(backend_run.hits[0].matched_profile_skills)
    assert backend_run.hits[0].matched_evidence
    assert backend_run.hits[0].rank_breakdown is not None
    assert backend_run.hits[0].rank_breakdown.profile_skills > 0
    assert program_run.hits[0].rank_breakdown is not None
    assert program_run.hits[0].rank_breakdown.profile_skills > 0


def test_smart_expansion_is_bounded_generated_from_target_and_persisted(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    connector = _Connector()
    service = JobDiscoveryService(jobintel_repo, {JobSource.BOSS: connector})

    run = service.discover(
        JobSearchPreference(
            candidate_id="C001",
            query="Agent开发",
            smart_expand=True,
            limit=2,
        )
    )

    assert run.preference.expanded_queries == ("AI Agent", "大模型应用开发")
    assert connector.preferences[0].expanded_queries == run.preference.expanded_queries
    assert run.profile_snapshot is not None
    assert run.profile_snapshot.expansion_queries == run.preference.expanded_queries
    restored = jobintel_repo.get_discovery_run(run.run_id)
    assert restored == run
    assert restored.hits[0].rank_breakdown is not None
    assert restored.hits[0].matched_evidence == run.hits[0].matched_evidence


def test_profile_skill_aliases_link_job_text_to_real_evidence(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    class AliasConnector:
        source = JobSource.BOSS

        def search(
            self, preference: JobSearchPreference, *, limit: int
        ) -> tuple[RawJobListing, ...]:
            del preference
            return (
                _listing(
                    "torch",
                    company="模型平台公司",
                    description="使用 Torch 和 K8s 部署模型服务",
                ),
            )[:limit]

    run = JobDiscoveryService(
        jobintel_repo,
        {JobSource.BOSS: AliasConnector()},
    ).discover(
        JobSearchPreference(candidate_id="C001", query="模型平台", limit=1),
        persist=False,
    )

    hit = run.hits[0]
    assert {"PyTorch", "Kubernetes"} <= set(hit.matched_profile_skills)
    assert {item.evidence_id for item in hit.matched_evidence} == {"ev-atlas-platform"}
