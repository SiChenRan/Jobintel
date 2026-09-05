from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobintel.guardrail import ViolationCode
from jobintel.models import (
    EvidenceMatchMethod,
    EvidenceSearchHit,
    JobAnalysisDraft,
    MatchStatus,
    RequirementMatchDraft,
    SearchCandidateEvidenceOutput,
)
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.provenance import EntityKind, EntityRef, ProvenanceLedger
from jobintel.scoring import derive_recommendation, score_requirements
from jobintel.services.analysis import (
    AnalysisFinalizationError,
    AnalysisService,
    AnalysisVersions,
    stable_analysis_id,
)

_NOW = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
_EVIDENCE_IDS = (
    "ev-python-skill",
    "ev-python-api",
    "ev-python-api",
    "ev-atlas-platform",
)


def _prepared(
    repo: SQLiteJobRepository, *, missing_last: bool = False
) -> tuple[ProvenanceLedger, JobAnalysisDraft]:
    job = repo.get_job("J001", 1)
    profile = repo.get_candidate_profile("C001", 2)
    evidence = {item.evidence_id: item for item in profile.evidence}
    ledger = ProvenanceLedger("run-finalizer")
    ledger.record_observation(
        tool_call_id="job",
        tool_name="get_job",
        tool_input={"job_id": job.job_id, "job_version": 1},
        tool_output=job,
        success=True,
        iteration=1,
        duration_ms=0,
        returned_entity_refs=(
            EntityRef(kind=EntityKind.JOB, entity_id=job.job_id, version=job.job_version),
        ),
    )
    ledger.record_observation(
        tool_call_id="profile",
        tool_name="get_candidate_profile",
        tool_input={"candidate_id": profile.candidate_id, "profile_version": 2},
        tool_output=profile,
        success=True,
        iteration=1,
        duration_ms=0,
        returned_entity_refs=(
            EntityRef(
                kind=EntityKind.CANDIDATE_PROFILE,
                entity_id=profile.candidate_id,
                version=profile.profile_version,
            ),
        ),
    )
    matches = []
    for index, (requirement, evidence_id) in enumerate(
        zip(job.requirements, _EVIDENCE_IDS, strict=True)
    ):
        selected = evidence[evidence_id]
        ledger.record_evidence_search(
            tool_call_id=f"search-{index}",
            tool_input={"query": requirement.text},
            output=SearchCandidateEvidenceOutput(
                job_id=job.job_id,
                job_version=job.job_version,
                requirement_id=requirement.requirement_id,
                candidate_id=profile.candidate_id,
                profile_version=profile.profile_version,
                query=requirement.text,
                hits=(
                    EvidenceSearchHit(
                        evidence=selected,
                        match_method=EvidenceMatchMethod.EXACT,
                        relevance_score=1,
                        matched_terms=("fixture",),
                    ),
                ),
            ),
            iteration=index + 2,
            duration_ms=0,
        )
        is_missing = missing_last and index == len(job.requirements) - 1
        matches.append(
            RequirementMatchDraft(
                requirement_id=requirement.requirement_id,
                status=MatchStatus.MISSING if is_missing else MatchStatus.MATCHED,
                evidence_ids=() if is_missing else (evidence_id,),
                confidence=0.9,
                reason="Deterministic fixture assessment.",
            )
        )
    return ledger, JobAnalysisDraft(
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        requirement_matches=tuple(matches),
        next_action="Tailor the resume and apply.",
    )


def test_finalizer_owns_score_recommendation_versions_and_persistence(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger, draft = _prepared(jobintel_repo, missing_last=True)
    versions = AnalysisVersions(
        prompt="prompt-test",
        parser="parser-test",
        toolset="toolset-test",
        scoring="scoring-test",
        schema="schema-test",
        provenance="provenance-test",
    )
    service = AnalysisService(jobintel_repo, ledger, versions=versions, clock=lambda: _NOW)

    analysis = service.finalize_and_save(draft)
    expected = score_requirements(
        jobintel_repo.get_job("J001", 1).requirements, draft.requirement_matches
    )

    assert analysis.analysis_id == stable_analysis_id(ledger.run_id)
    assert analysis.run_id == ledger.run_id
    assert analysis.score_breakdown == expected
    assert analysis.score == expected.score
    assert analysis.recommendation == derive_recommendation(expected.score)
    assert analysis.created_at == _NOW
    assert analysis.provenance_digest == ledger.snapshot().digest
    assert analysis.missing_skills[0].requirement_id == draft.requirement_matches[-1].requirement_id
    assert (
        analysis.prompt_version,
        analysis.parser_version,
        analysis.toolset_version,
        analysis.scoring_version,
        analysis.schema_version,
        analysis.provenance_version,
    ) == tuple(versions.__dict__.values())
    assert jobintel_repo.get_analysis(analysis.analysis_id) == analysis


def test_finalizer_is_idempotent_for_same_semantic_run(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger, draft = _prepared(jobintel_repo)
    service = AnalysisService(jobintel_repo, ledger)

    assert service.finalize_and_save(draft) == service.finalize_and_save(draft)


def test_guardrail_rejection_happens_before_any_write(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    _ledger, draft = _prepared(jobintel_repo)
    empty_ledger = ProvenanceLedger("run-rejected")

    with pytest.raises(AnalysisFinalizationError) as exc:
        AnalysisService(jobintel_repo, empty_ledger).finalize_and_save(draft)

    codes = {violation.code for violation in exc.value.violations}
    assert codes >= {
        ViolationCode.JOB_NOT_OBSERVED,
        ViolationCode.PROFILE_NOT_OBSERVED,
        ViolationCode.MISSING_EVIDENCE_SEARCH,
    }
    with pytest.raises(Exception, match="analysis not found"):
        jobintel_repo.get_analysis(stable_analysis_id("run-rejected"))


def test_stable_analysis_id_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        stable_analysis_id("  ")
