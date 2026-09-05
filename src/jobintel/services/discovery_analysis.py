"""Deep analysis orchestration for jobs already captured by discovery."""

from __future__ import annotations

import asyncio

from jobintel.agent.core import JobIntelAgent, JobIntelAgentError
from jobintel.config import JobIntelSettings
from jobintel.discovery.models import DiscoveryRun
from jobintel.errors import JobIntelError
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.providers.base import LLMProvider
from jobintel.providers.factory import build_jobintel_provider
from jobintel.services.intake import AnalysisRequest


def analyze_discovery_hits(
    run: DiscoveryRun,
    *,
    analyze_top: int,
    repository: SQLiteJobRepository,
    settings: JobIntelSettings,
    dry_run: bool,
    llm: LLMProvider | None = None,
) -> list[dict[str, object]]:
    """Analyze ranked saved jobs without returning to their source platform."""
    if analyze_top == 0:
        return []
    selected_llm = llm or build_jobintel_provider(settings)
    agent = JobIntelAgent(selected_llm, repository, settings)

    async def analyze_hits() -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for hit in run.hits[:analyze_top]:
            job = hit.job
            jd_text = "\n".join(
                value
                for value in (
                    job.title,
                    f"公司: {job.company_name}",
                    f"地点: {job.location}" if job.location else "",
                    f"薪资: {job.salary_text}" if job.salary_text else "",
                    f"经验: {job.experience}" if job.experience else "",
                    f"学历: {job.education}" if job.education else "",
                    f"技能: {', '.join(job.skills)}" if job.skills else "",
                    job.description,
                )
                if value
            )
            request = AnalysisRequest(
                candidate_id=run.preference.candidate_id,
                profile_version=run.preference.profile_version,
                jd_text=jd_text,
                jd_source_url=str(job.source_links[0].url),
            )
            try:
                result = await agent.analyze(request, dry_run=dry_run)
            except (JobIntelAgentError, JobIntelError, RuntimeError, ValueError) as exc:
                values.append(
                    {
                        "discovery_job_id": job.discovery_job_id,
                        "error": {"code": "ANALYZE_FAILED", "message": str(exc)},
                    }
                )
            else:
                values.append(
                    {
                        "discovery_job_id": job.discovery_job_id,
                        "analysis": result.analysis.model_dump(mode="json"),
                    }
                )
        return values

    return asyncio.run(analyze_hits())
