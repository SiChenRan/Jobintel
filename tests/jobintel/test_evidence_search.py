"""Tests for deterministic scoped candidate evidence retrieval."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobintel.errors import EvidenceSearchError
from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceMatchMethod,
    EvidenceType,
    JobPosting,
    JobRequirement,
    RequirementCategory,
    RequirementImportance,
)
from jobintel.services.evidence_search import EvidenceSearchService, normalize_search_text

_NOW = datetime(2026, 9, 4, tzinfo=UTC)
_HASH = "a" * 64


def _requirement(
    requirement_id: str = "req-k8s", *, text: str = "Deploy container platforms"
) -> JobRequirement:
    return JobRequirement(
        requirement_id=requirement_id,
        text=text,
        category=RequirementCategory.SKILL,
        importance=RequirementImportance.MUST,
        normalized_skill="Kubernetes",
        source_order=0,
    )


def _evidence(
    evidence_id: str,
    title: str,
    content: str,
    *,
    skills: tuple[str, ...] = (),
    source_order: int,
) -> CandidateEvidence:
    return CandidateEvidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.PROJECT,
        title=title,
        content=content,
        skills=skills,
        source_order=source_order,
    )


def _scope(
    evidence: tuple[CandidateEvidence, ...], requirement: JobRequirement | None = None
) -> tuple[JobPosting, JobRequirement, CandidateProfile]:
    requirement = requirement or _requirement()
    job = JobPosting(
        job_id="J-search",
        job_version=1,
        company_name="Example",
        title="Platform Engineer",
        description="Operate container platforms.",
        requirements=(requirement,),
        source_sha256=_HASH,
        created_at=_NOW,
    )
    profile = CandidateProfile(
        candidate_id="C-search",
        profile_version=2,
        evidence=evidence,
        source_sha256=_HASH,
        created_at=_NOW,
    )
    return job, requirement, profile


def test_search_orders_exact_alias_lexical_then_fuzzy() -> None:
    evidence = (
        _evidence(
            "ev-exact",
            "K8s platform",
            "Operated clusters.",
            source_order=3,
        ),
        _evidence(
            "ev-alias",
            "Kubernetes platform",
            "Operated clusters.",
            source_order=2,
        ),
        _evidence(
            "ev-lexical",
            "Container platform",
            "Led reliable platform deployments.",
            source_order=1,
        ),
    )
    requirement = _requirement().model_copy(update={"normalized_skill": None})
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job, requirement=requirement, profile=profile, query="k8s", top_k=5
    )

    assert [hit.evidence.evidence_id for hit in result.hits] == [
        "ev-exact",
        "ev-alias",
        "ev-lexical",
    ]
    assert [hit.match_method for hit in result.hits] == [
        EvidenceMatchMethod.EXACT,
        EvidenceMatchMethod.ALIAS,
        EvidenceMatchMethod.LEXICAL,
    ]
    assert result.candidate_id == "C-search"
    assert result.profile_version == 2


def test_fuzzy_search_handles_typo_without_promoting_unrelated_evidence() -> None:
    requirement = _requirement(text="Cluster orchestration")
    evidence = (
        _evidence(
            "ev-fuzzy",
            "Container operations",
            "Used Kubernetes in production.",
            source_order=0,
        ),
        _evidence(
            "ev-unrelated",
            "Financial reporting",
            "Prepared quarterly budgets.",
            source_order=1,
        ),
    )
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job,
        requirement=requirement.model_copy(update={"normalized_skill": None}),
        profile=profile,
        query="Kubernets",
    )

    assert len(result.hits) == 1
    assert result.hits[0].evidence.evidence_id == "ev-fuzzy"
    assert result.hits[0].match_method is EvidenceMatchMethod.FUZZY


def test_stable_tie_break_and_top_k_use_source_order_then_id() -> None:
    evidence = tuple(
        _evidence(
            f"ev-{index}",
            "Python service",
            "Built Python APIs.",
            skills=("Python",),
            source_order=index,
        )
        for index in (2, 0, 1)
    )
    requirement = _requirement(text="Python")
    requirement = requirement.model_copy(update={"normalized_skill": "Python"})
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job, requirement=requirement, profile=profile, query="Python", top_k=2
    )

    assert [hit.evidence.evidence_id for hit in result.hits] == ["ev-0", "ev-1"]


def test_search_does_not_match_short_term_inside_larger_word() -> None:
    evidence = (
        _evidence(
            "ev-google",
            "Google Cloud",
            "Built services on Google Cloud.",
            source_order=0,
        ),
    )
    requirement = _requirement(text="Go programming")
    requirement = requirement.model_copy(update={"normalized_skill": "Go"})
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job, requirement=requirement, profile=profile, query="Go"
    )

    assert result.hits == ()


def test_search_handles_evidence_with_no_meaningful_tokens() -> None:
    evidence = (
        _evidence(
            "ev-stop-words",
            "and",
            "the",
            source_order=0,
        ),
    )
    requirement = _requirement(text="and").model_copy(update={"normalized_skill": None})
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job, requirement=requirement, profile=profile, query="Rust"
    )

    assert result.hits == ()


def test_search_is_limited_to_supplied_profile_version(
    jobintel_repo: object,
) -> None:
    repository = jobintel_repo
    job = repository.get_job("J001", 1)  # type: ignore[attr-defined]
    version_one = repository.get_candidate_profile("C001", 1)  # type: ignore[attr-defined]
    result = EvidenceSearchService().search(
        job=job,
        requirement=job.requirements[3],
        profile=version_one,
        query="Kubernetes",
    )
    assert "ev-atlas-platform" not in {hit.evidence.evidence_id for hit in result.hits}


@pytest.mark.parametrize("top_k", [0, 21])
def test_search_rejects_invalid_top_k(top_k: int) -> None:
    job, requirement, profile = _scope(())
    with pytest.raises(EvidenceSearchError, match="top_k"):
        EvidenceSearchService().search(
            job=job, requirement=requirement, profile=profile, query="Python", top_k=top_k
        )


def test_search_rejects_empty_query_and_wrong_requirement_scope() -> None:
    job, requirement, profile = _scope(())
    with pytest.raises(EvidenceSearchError, match="query"):
        EvidenceSearchService().search(
            job=job, requirement=requirement, profile=profile, query="  "
        )
    outside_requirement = requirement.model_copy(update={"requirement_id": "req-outside"})
    with pytest.raises(EvidenceSearchError, match="does not belong"):
        EvidenceSearchService().search(
            job=job,
            requirement=outside_requirement,
            profile=profile,
            query="Kubernetes",
        )


def test_alias_configuration_and_text_normalization_are_validated() -> None:
    assert normalize_search_text("  \uff2b\uff18\uff33\nPlatform ") == "k8s platform"
    with pytest.raises(ValueError, match="aliases must not be empty"):
        EvidenceSearchService(aliases={"": ("valid",)})


def test_alias_is_detected_inside_a_natural_language_query() -> None:
    evidence = (
        _evidence(
            "ev-kubernetes",
            "Kubernetes operations",
            "Operated production clusters.",
            source_order=0,
        ),
    )
    requirement = _requirement().model_copy(update={"normalized_skill": None})
    job, requirement, profile = _scope(evidence, requirement)

    result = EvidenceSearchService().search(
        job=job,
        requirement=requirement,
        profile=profile,
        query="Has hands-on k8s deployment work",
    )

    assert result.hits[0].match_method is EvidenceMatchMethod.ALIAS
