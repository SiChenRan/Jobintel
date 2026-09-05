"""Tests for content-free, run-scoped provenance recording."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jobintel.models import (
    CandidateEvidence,
    EvidenceMatchMethod,
    EvidenceSearchHit,
    EvidenceType,
    SearchCandidateEvidenceOutput,
)
from jobintel.provenance import (
    EntityKind,
    EntityRef,
    EvidenceSearchScope,
    ProvenanceLedger,
    ToolObservation,
    canonical_sha256,
    evidence_content_sha256,
)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _evidence(content: str = "Built Python services.") -> CandidateEvidence:
    return CandidateEvidence(
        evidence_id="ev-python",
        evidence_type=EvidenceType.EXPERIENCE,
        title="Backend Engineer",
        content=content,
        skills=("Python",),
        source_order=0,
    )


def _search_output(*, hits: bool = True) -> SearchCandidateEvidenceOutput:
    evidence_hits = (
        (
            EvidenceSearchHit(
                evidence=_evidence(),
                match_method=EvidenceMatchMethod.EXACT,
                relevance_score=1,
                matched_terms=("python",),
            ),
        )
        if hits
        else ()
    )
    return SearchCandidateEvidenceOutput(
        job_id="J001",
        job_version=1,
        requirement_id="req-python",
        candidate_id="C001",
        profile_version=2,
        query="Python",
        hits=evidence_hits,
    )


def test_canonical_hash_is_order_independent_and_content_hash_is_exact() -> None:
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})
    assert evidence_content_sha256(_evidence()) != evidence_content_sha256(
        _evidence("Built Python services!")
    )


def test_generic_observation_records_only_hashes_and_entity_refs() -> None:
    ledger = ProvenanceLedger(" run-1 ")
    reference = EntityRef(kind=EntityKind.JOB, entity_id="J001", version=1)
    observation = ledger.record_observation(
        tool_call_id="call-job",
        tool_name="get_job",
        tool_input={"job_id": "J001"},
        tool_output={"description": "private job content"},
        success=True,
        iteration=1,
        duration_ms=4,
        returned_entity_refs=(reference,),
    )

    assert ledger.run_id == "run-1"
    assert ledger.has_entity(reference) is True
    assert observation.input_sha256 != observation.output_sha256
    assert "private job content" not in observation.model_dump_json()


def test_failed_observation_requires_error_code_and_does_not_observe_entities() -> None:
    ledger = ProvenanceLedger("run-1")
    reference = EntityRef(kind=EntityKind.JOB, entity_id="J001", version=1)
    ledger.record_observation(
        tool_call_id="call-job",
        tool_name="get_job",
        tool_input={"job_id": "J001"},
        tool_output={"code": "NOT_FOUND"},
        success=False,
        error_code="NOT_FOUND",
        iteration=1,
        duration_ms=1,
        returned_entity_refs=(reference,),
    )
    assert ledger.has_entity(reference) is False

    with pytest.raises(ValidationError, match="requires error_code"):
        ToolObservation(
            tool_call_id="bad",
            tool_name="get_job",
            input_sha256="a" * 64,
            output_sha256="b" * 64,
            success=False,
            iteration=1,
            duration_ms=0,
        )
    with pytest.raises(ValidationError, match="must not contain error_code"):
        ToolObservation(
            tool_call_id="bad",
            tool_name="get_job",
            input_sha256="a" * 64,
            output_sha256="b" * 64,
            success=True,
            error_code="IMPOSSIBLE",
            iteration=1,
            duration_ms=0,
        )


def test_evidence_search_creates_scoped_receipts_and_supports_empty_results() -> None:
    ledger = ProvenanceLedger("run-1")
    observation, receipts = ledger.record_evidence_search(
        tool_call_id="call-search",
        tool_input={"query": "Python"},
        output=_search_output(),
        iteration=2,
        duration_ms=8,
    )
    scope = EvidenceSearchScope(
        job_id="J001",
        job_version=1,
        requirement_id="req-python",
        candidate_id="C001",
        profile_version=2,
    )

    assert observation.evidence_search_scope == scope
    assert ledger.has_successful_search(scope) is True
    assert ledger.receipts_for_scope(scope) == receipts
    assert ledger.receipts_for_evidence("ev-python") == receipts
    assert receipts[0].scope == scope
    assert receipts[0].content_sha256 == evidence_content_sha256(_evidence())

    empty_output = _search_output(hits=False).model_copy(update={"requirement_id": "req-empty"})
    ledger.record_evidence_search(
        tool_call_id="call-empty",
        tool_input={"query": "Rust"},
        output=empty_output,
        iteration=3,
        duration_ms=2,
    )
    empty_scope = scope.model_copy(update={"requirement_id": "req-empty"})
    assert ledger.has_successful_search(empty_scope) is True
    assert ledger.receipts_for_scope(empty_scope) == ()


def test_duplicate_tool_calls_are_rejected_without_partial_receipts() -> None:
    ledger = ProvenanceLedger("run-1")
    ledger.record_evidence_search(
        tool_call_id="duplicate",
        tool_input={"query": "Python"},
        output=_search_output(),
        iteration=1,
        duration_ms=1,
    )
    with pytest.raises(ValueError, match="duplicate tool_call_id"):
        ledger.record_evidence_search(
            tool_call_id="duplicate",
            tool_input={"query": "Python"},
            output=_search_output(),
            iteration=2,
            duration_ms=1,
        )
    assert len(ledger.observations) == 1
    assert len(ledger.evidence_receipts) == 1


def test_snapshot_digest_is_stable_and_contains_no_evidence_content() -> None:
    ledger = ProvenanceLedger("run-1")
    ledger.record_evidence_search(
        tool_call_id="call-search",
        tool_input={"query": "Python"},
        output=_search_output(),
        iteration=1,
        duration_ms=1,
    )

    first = ledger.snapshot()
    second = ledger.snapshot()

    assert first == second
    assert first.digest == second.digest
    assert "Built Python services" not in first.model_dump_json()


def test_provenance_models_reject_invalid_run_and_entity_versions() -> None:
    with pytest.raises(ValueError, match="run_id"):
        ProvenanceLedger(" ")
    with pytest.raises(ValidationError, match="requires a version"):
        EntityRef(kind=EntityKind.CANDIDATE_PROFILE, entity_id="C001")
    company = EntityRef(kind=EntityKind.COMPANY, entity_id="co-1")
    assert company.version is None
