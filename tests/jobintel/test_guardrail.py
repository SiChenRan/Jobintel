"""Adversarial tests for requirement-scoped provenance guardrails."""

from __future__ import annotations

from jobintel.guardrail import ViolationCode, validate_analysis_draft
from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceMatchMethod,
    EvidenceSearchHit,
    GroundedClaim,
    InterviewTopic,
    JobAnalysisDraft,
    JobPosting,
    MatchStatus,
    RequirementMatchDraft,
    ResumeSuggestion,
    SearchCandidateEvidenceOutput,
)
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.provenance import EntityKind, EntityRef, ProvenanceLedger


def _evidence_map(profile: CandidateProfile) -> dict[str, CandidateEvidence]:
    return {item.evidence_id: item for item in profile.evidence}


def _selected_evidence(job: JobPosting, profile: CandidateProfile) -> dict[str, CandidateEvidence]:
    evidence = _evidence_map(profile)
    evidence_ids = (
        "ev-python-skill",
        "ev-python-api",
        "ev-python-api",
        "ev-atlas-platform",
    )
    return {
        requirement.requirement_id: evidence[evidence_id]
        for requirement, evidence_id in zip(job.requirements, evidence_ids, strict=True)
    }


def _ledger(
    job: JobPosting,
    profile: CandidateProfile,
    *,
    observe_job: bool = True,
    observe_profile: bool = True,
    omit_search_for: str | None = None,
    evidence_overrides: dict[str, CandidateEvidence] | None = None,
) -> ProvenanceLedger:
    ledger = ProvenanceLedger("run-guardrail")
    if observe_job:
        ledger.record_observation(
            tool_call_id="get-job",
            tool_name="get_job",
            tool_input={"job_id": job.job_id, "job_version": job.job_version},
            tool_output=job,
            success=True,
            iteration=1,
            duration_ms=1,
            returned_entity_refs=(
                EntityRef(kind=EntityKind.JOB, entity_id=job.job_id, version=job.job_version),
            ),
        )
    if observe_profile:
        ledger.record_observation(
            tool_call_id="get-profile",
            tool_name="get_candidate_profile",
            tool_input={
                "candidate_id": profile.candidate_id,
                "profile_version": profile.profile_version,
            },
            tool_output=profile,
            success=True,
            iteration=1,
            duration_ms=1,
            returned_entity_refs=(
                EntityRef(
                    kind=EntityKind.CANDIDATE_PROFILE,
                    entity_id=profile.candidate_id,
                    version=profile.profile_version,
                ),
            ),
        )

    selected = _selected_evidence(job, profile)
    selected.update(evidence_overrides or {})
    for index, requirement in enumerate(job.requirements):
        if requirement.requirement_id == omit_search_for:
            continue
        evidence = selected[requirement.requirement_id]
        output = SearchCandidateEvidenceOutput(
            job_id=job.job_id,
            job_version=job.job_version,
            requirement_id=requirement.requirement_id,
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
            query=requirement.text,
            hits=(
                EvidenceSearchHit(
                    evidence=evidence,
                    match_method=EvidenceMatchMethod.EXACT,
                    relevance_score=1,
                    matched_terms=("fixture",),
                ),
            ),
        )
        ledger.record_evidence_search(
            tool_call_id=f"search-{index}",
            tool_input={
                "requirement_id": requirement.requirement_id,
                "candidate_id": profile.candidate_id,
            },
            output=output,
            iteration=index + 2,
            duration_ms=2,
        )
    return ledger


def _draft(job: JobPosting, profile: CandidateProfile) -> JobAnalysisDraft:
    selected = _selected_evidence(job, profile)
    matches = tuple(
        RequirementMatchDraft(
            requirement_id=requirement.requirement_id,
            status=MatchStatus.MATCHED,
            evidence_ids=(selected[requirement.requirement_id].evidence_id,),
            confidence=0.9,
            reason="Fixture match.",
        )
        for requirement in job.requirements
    )
    first = matches[0]
    return JobAnalysisDraft(
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        requirement_matches=matches,
        strengths=(
            GroundedClaim(
                claim_id="strength-1",
                text="Supported strength.",
                requirement_ids=(first.requirement_id,),
                evidence_ids=first.evidence_ids,
            ),
        ),
        resume_suggestions=(
            ResumeSuggestion(
                requirement_id=matches[1].requirement_id,
                text="Quantify the production API work.",
            ),
        ),
        interview_topics=(
            InterviewTopic(
                requirement_id=matches[2].requirement_id,
                text="Discuss PostgreSQL trade-offs.",
            ),
        ),
        next_action="Tailor the resume and apply.",
    )


def _codes(result: object) -> set[ViolationCode]:
    return {item.code for item in result.violations}  # type: ignore[attr-defined]


def test_valid_draft_passes_all_guardrails(jobintel_repo: SQLiteJobRepository) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    result = validate_analysis_draft(
        draft=_draft(job, profile),
        job=job,
        profile=profile,
        ledger=_ledger(job, profile),
    )

    assert result.is_valid is True
    assert result.violations == ()


def test_draft_and_observation_scope_mismatches_are_structured(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    draft = _draft(job, profile).model_copy(update={"job_id": "J-wrong", "candidate_id": "C-wrong"})
    ledger = _ledger(job, profile, observe_job=False, observe_profile=False)

    result = validate_analysis_draft(draft=draft, job=job, profile=profile, ledger=ledger)

    assert _codes(result) >= {
        ViolationCode.DRAFT_JOB_SCOPE_MISMATCH,
        ViolationCode.DRAFT_PROFILE_SCOPE_MISMATCH,
        ViolationCode.JOB_NOT_OBSERVED,
        ViolationCode.PROFILE_NOT_OBSERVED,
    }


def test_missing_duplicate_and_unknown_requirements_are_rejected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)
    unknown = valid.requirement_matches[0].model_copy(update={"requirement_id": "req-invented"})
    matches = (
        valid.requirement_matches[0],
        valid.requirement_matches[0],
        unknown,
        *valid.requirement_matches[2:],
    )
    draft = valid.model_copy(update={"requirement_matches": matches, "strengths": ()})

    result = validate_analysis_draft(
        draft=draft, job=job, profile=profile, ledger=_ledger(job, profile)
    )

    assert _codes(result) >= {
        ViolationCode.DUPLICATE_REQUIREMENT_MATCH,
        ViolationCode.MISSING_REQUIREMENT_MATCH,
        ViolationCode.UNKNOWN_REQUIREMENT,
    }


def test_every_requirement_requires_a_successful_scoped_search(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    omitted = job.requirements[2].requirement_id
    result = validate_analysis_draft(
        draft=_draft(job, profile),
        job=job,
        profile=profile,
        ledger=_ledger(job, profile, omit_search_for=omitted),
    )

    violations = [
        item for item in result.violations if item.code is ViolationCode.MISSING_EVIDENCE_SEARCH
    ]
    assert [item.requirement_id for item in violations] == [omitted]


def test_forged_and_wrong_requirement_evidence_are_rejected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)

    forged_match = valid.requirement_matches[0].model_copy(update={"evidence_ids": ("ev-forged",)})
    forged = valid.model_copy(
        update={
            "requirement_matches": (forged_match, *valid.requirement_matches[1:]),
            "strengths": (),
        }
    )
    forged_result = validate_analysis_draft(
        draft=forged, job=job, profile=profile, ledger=_ledger(job, profile)
    )
    assert ViolationCode.EVIDENCE_NOT_RETURNED in _codes(forged_result)

    wrong_requirement_match = valid.requirement_matches[0].model_copy(
        update={"evidence_ids": valid.requirement_matches[3].evidence_ids}
    )
    wrong_requirement = valid.model_copy(
        update={
            "requirement_matches": (
                wrong_requirement_match,
                *valid.requirement_matches[1:],
            ),
            "strengths": (),
        }
    )
    wrong_result = validate_analysis_draft(
        draft=wrong_requirement,
        job=job,
        profile=profile,
        ledger=_ledger(job, profile),
    )
    assert ViolationCode.EVIDENCE_SCOPE_MISMATCH in _codes(wrong_result)


def test_cross_candidate_and_stale_profile_receipts_do_not_escape_scope(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)
    ledger = _ledger(job, profile)
    other_profile = jobintel_repo.get_candidate_profile("C002", 1)
    other_evidence = other_profile.evidence[0]
    requirement = job.requirements[0]
    cross_output = SearchCandidateEvidenceOutput(
        job_id=job.job_id,
        job_version=job.job_version,
        requirement_id=requirement.requirement_id,
        candidate_id=other_profile.candidate_id,
        profile_version=other_profile.profile_version,
        query=requirement.text,
        hits=(
            EvidenceSearchHit(
                evidence=other_evidence,
                match_method=EvidenceMatchMethod.LEXICAL,
                relevance_score=0.5,
            ),
        ),
    )
    ledger.record_evidence_search(
        tool_call_id="cross-candidate",
        tool_input={"candidate_id": other_profile.candidate_id},
        output=cross_output,
        iteration=9,
        duration_ms=1,
    )
    cross_match = valid.requirement_matches[0].model_copy(
        update={"evidence_ids": (other_evidence.evidence_id,)}
    )
    draft = valid.model_copy(
        update={
            "requirement_matches": (cross_match, *valid.requirement_matches[1:]),
            "strengths": (),
        }
    )

    result = validate_analysis_draft(draft=draft, job=job, profile=profile, ledger=ledger)

    assert ViolationCode.EVIDENCE_SCOPE_MISMATCH in _codes(result)

    stale_profile = jobintel_repo.get_candidate_profile("C001", 1)
    stale_evidence = _evidence_map(stale_profile)["ev-python-api"]
    stale_output = cross_output.model_copy(
        update={
            "candidate_id": stale_profile.candidate_id,
            "profile_version": stale_profile.profile_version,
            "hits": (
                EvidenceSearchHit(
                    evidence=stale_evidence,
                    match_method=EvidenceMatchMethod.EXACT,
                    relevance_score=1,
                ),
            ),
        }
    )
    ledger.record_evidence_search(
        tool_call_id="stale-profile",
        tool_input={"profile_version": 1},
        output=stale_output,
        iteration=10,
        duration_ms=1,
    )
    stale_match = valid.requirement_matches[0].model_copy(
        update={"evidence_ids": (stale_evidence.evidence_id,)}
    )
    stale_draft = valid.model_copy(
        update={
            "requirement_matches": (stale_match, *valid.requirement_matches[1:]),
            "strengths": (),
        }
    )
    stale_result = validate_analysis_draft(
        draft=stale_draft, job=job, profile=profile, ledger=ledger
    )
    assert ViolationCode.EVIDENCE_SCOPE_MISMATCH in _codes(stale_result)


def test_receipt_evidence_must_exist_unchanged_in_profile(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    requirement_id = job.requirements[0].requirement_id
    selected = _selected_evidence(job, profile)[requirement_id]
    changed = selected.model_copy(update={"content": "Tampered content"})
    content_ledger = _ledger(job, profile, evidence_overrides={requirement_id: changed})
    content_result = validate_analysis_draft(
        draft=_draft(job, profile), job=job, profile=profile, ledger=content_ledger
    )
    assert ViolationCode.EVIDENCE_CONTENT_MISMATCH in _codes(content_result)

    invented = selected.model_copy(update={"evidence_id": "ev-not-in-profile"})
    missing_ledger = _ledger(job, profile, evidence_overrides={requirement_id: invented})
    valid = _draft(job, profile)
    invented_match = valid.requirement_matches[0].model_copy(
        update={"evidence_ids": (invented.evidence_id,)}
    )
    invented_draft = valid.model_copy(
        update={
            "requirement_matches": (invented_match, *valid.requirement_matches[1:]),
            "strengths": (),
        }
    )
    missing_result = validate_analysis_draft(
        draft=invented_draft, job=job, profile=profile, ledger=missing_ledger
    )
    assert ViolationCode.EVIDENCE_NOT_IN_PROFILE in _codes(missing_result)


def test_match_status_evidence_rules_are_rechecked_after_model_boundary(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)
    no_evidence = valid.requirement_matches[0].model_copy(update={"evidence_ids": ()})
    missing_with_evidence = valid.requirement_matches[1].model_copy(
        update={"status": MatchStatus.MISSING}
    )
    draft = valid.model_copy(
        update={
            "requirement_matches": (
                no_evidence,
                missing_with_evidence,
                *valid.requirement_matches[2:],
            ),
            "strengths": (),
        }
    )

    result = validate_analysis_draft(
        draft=draft, job=job, profile=profile, ledger=_ledger(job, profile)
    )

    assert _codes(result) >= {
        ViolationCode.POSITIVE_MATCH_WITHOUT_EVIDENCE,
        ViolationCode.MISSING_MATCH_WITH_EVIDENCE,
    }


def test_strength_must_use_matched_requirement_and_its_evidence_subset(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)
    partial = valid.requirement_matches[0].model_copy(update={"status": MatchStatus.PARTIAL})
    bad_strength = valid.strengths[0].model_copy(
        update={"evidence_ids": valid.requirement_matches[1].evidence_ids}
    )
    draft = valid.model_copy(
        update={
            "requirement_matches": (partial, *valid.requirement_matches[1:]),
            "strengths": (bad_strength,),
        }
    )

    result = validate_analysis_draft(
        draft=draft, job=job, profile=profile, ledger=_ledger(job, profile)
    )

    assert _codes(result) >= {
        ViolationCode.STRENGTH_REQUIRES_MATCHED_REQUIREMENT,
        ViolationCode.STRENGTH_EVIDENCE_MISMATCH,
    }


def test_resume_and_interview_items_must_reference_real_requirements(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job = jobintel_repo.get_job("J001", 1)
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    valid = _draft(job, profile)
    draft = valid.model_copy(
        update={
            "resume_suggestions": (
                ResumeSuggestion(requirement_id="req-fake", text="Invented suggestion."),
            ),
            "interview_topics": (
                InterviewTopic(requirement_id="req-fake-2", text="Invented topic."),
            ),
        }
    )

    result = validate_analysis_draft(
        draft=draft, job=job, profile=profile, ledger=_ledger(job, profile)
    )

    violations = [
        item
        for item in result.violations
        if item.code is ViolationCode.UNKNOWN_NARRATIVE_REQUIREMENT
    ]
    assert len(violations) == 2
    serialized = " ".join(item.message for item in violations)
    assert "Quantify the production API work" not in serialized
