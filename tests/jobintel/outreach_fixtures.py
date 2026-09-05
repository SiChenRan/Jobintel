"""Reusable builders for outreach unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from jobintel.models import (
    CandidateProfile,
    JobAnalysis,
    JobPosting,
    MatchStatus,
    RequirementMatchDraft,
)
from jobintel.outreach.models import OutreachClaimDraft, OutreachMessageDraft
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.scoring import derive_recommendation, score_requirements


def build_outreach_scope(
    repository: SQLiteJobRepository,
) -> tuple[JobPosting, CandidateProfile, JobAnalysis]:
    """Build one valid analyzed job/profile scope from versioned fixtures."""
    job = repository.get_job("J001", 1)
    profile = repository.get_candidate_profile("C001", 2)
    evidence_ids = (
        "ev-python-skill",
        "ev-python-api",
        "ev-python-api",
        None,
    )
    statuses = (
        MatchStatus.MATCHED,
        MatchStatus.MATCHED,
        MatchStatus.PARTIAL,
        MatchStatus.MISSING,
    )
    matches = tuple(
        RequirementMatchDraft(
            requirement_id=requirement.requirement_id,
            status=status,
            evidence_ids=(evidence_id,) if evidence_id else (),
            confidence=0.9,
            reason="候选人证据与岗位要求的测试匹配结果。",
        )
        for requirement, status, evidence_id in zip(
            job.requirements, statuses, evidence_ids, strict=True
        )
    )
    breakdown = score_requirements(job.requirements, matches)
    analysis = JobAnalysis(
        analysis_id="analysis-outreach-fixture",
        run_id="run-outreach-fixture",
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        requirement_matches=matches,
        strengths=(),
        resume_suggestions=(),
        interview_topics=(),
        next_action="建议审核沟通文案后再决定是否联系招聘者。",
        score=breakdown.score,
        recommendation=derive_recommendation(breakdown.score),
        score_breakdown=breakdown,
        missing_skills=(),
        prompt_version="test-analysis-prompt",
        parser_version="test-parser",
        toolset_version="test-toolset",
        scoring_version="test-scoring",
        schema_version="test-analysis-schema",
        provenance_version="test-provenance",
        provenance_digest="0" * 64,
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    return job, profile, analysis


def valid_outreach_message(job: JobPosting) -> OutreachMessageDraft:
    """Return a concise valid message for the first matched requirement."""
    requirement = job.requirements[0]
    return OutreachMessageDraft(
        salutation="招聘负责人您好",
        motivation=f"我对贵公司的{job.title}岗位很感兴趣。",
        claims=(
            OutreachClaimDraft(
                text="我有 Python 后端开发和生产问题排查经验。",
                requirement_ids=(requirement.requirement_id,),
                evidence_ids=("ev-python-skill",),
            ),
        ),
        conversation_opener="想了解这个岗位当前最需要解决的技术问题是什么?",
        closing="期待与您进一步交流。",
    )
