from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from jobintel.agent.tools import JobIntelToolbox, ToolExecutionError
from jobintel.errors import IdempotencyConflictError, PersistenceValidationError
from jobintel.mcp_server import build_server
from jobintel.models import JobAnalysisDraft, MatchStatus, RequirementMatchDraft
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.provenance import EvidenceSearchScope, ProvenanceLedger
from jobintel.providers.base import ToolCall
from jobintel.tool_contracts import ToolErrorCode


class _FailingAnalysisService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def finalize_and_save(self, _draft: JobAnalysisDraft) -> None:
        raise self._error


async def _researched_draft(
    toolbox: JobIntelToolbox,
) -> JobAnalysisDraft:
    job = await toolbox.execute(
        "get_job", {"job_id": "J001", "job_version": 1}, tool_call_id="job", iteration=1
    )
    profile = await toolbox.execute(
        "get_candidate_profile",
        {"candidate_id": "C001", "profile_version": 2},
        tool_call_id="profile",
        iteration=1,
    )
    matches = []
    for index, requirement in enumerate(job.requirements):  # type: ignore[attr-defined]
        result = await toolbox.execute(
            "search_candidate_evidence",
            {
                "job_id": "J001",
                "job_version": 1,
                "requirement_id": requirement.requirement_id,
                "candidate_id": "C001",
                "profile_version": 2,
                "query": requirement.normalized_skill or requirement.text,
            },
            tool_call_id=f"search-{index}",
            iteration=index + 2,
        )
        hit = result.hits[0]  # type: ignore[attr-defined]
        matches.append(
            RequirementMatchDraft(
                requirement_id=requirement.requirement_id,
                status=MatchStatus.MATCHED,
                evidence_ids=(hit.evidence.evidence_id,),
                confidence=0.9,
                reason="Evidence found by the scoped search.",
            )
        )
    return JobAnalysisDraft(
        job_id=job.job_id,  # type: ignore[attr-defined]
        job_version=job.job_version,  # type: ignore[attr-defined]
        candidate_id=profile.candidate_id,  # type: ignore[attr-defined]
        profile_version=profile.profile_version,  # type: ignore[attr-defined]
        requirement_matches=tuple(matches),
        next_action="Tailor the resume and apply.",
    )


async def test_toolbox_runs_full_research_and_terminal_write(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger = ProvenanceLedger("run-toolbox")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)
    draft = await _researched_draft(toolbox)

    parsed = await toolbox.execute(
        "parse_job_requirements",
        {"job_id": "J001", "job_version": 1},
        tool_call_id="parse",
        iteration=7,
    )
    company = await toolbox.execute(
        "get_company", {"company_id": "co-orbit"}, tool_call_id="company", iteration=7
    )
    saved = await toolbox.execute(
        "save_application_analysis",
        draft.model_dump(mode="json"),
        tool_call_id="save",
        iteration=8,
    )

    assert len(parsed.requirements) == len(draft.requirement_matches)  # type: ignore[attr-defined]
    assert company.company_id == "co-orbit"  # type: ignore[attr-defined]
    analysis = saved.analysis  # type: ignore[attr-defined]
    assert analysis.score == 100
    assert jobintel_repo.get_analysis(analysis.analysis_id) == analysis
    assert len(ledger.observations) == 9
    assert len(ledger.evidence_receipts) >= 4
    assert ledger.observations[-1].tool_name == "save_application_analysis"


async def test_dispatch_returns_structured_success_and_failure(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger = ProvenanceLedger("run-dispatch")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)

    success = await toolbox.dispatch(
        ToolCall(id="ok", name="get_job", arguments={"job_id": "J001"}), iteration=1
    )
    failure = await toolbox.dispatch(ToolCall(id="bad", name="get_job", arguments={}), iteration=2)

    assert success.is_error is False
    assert json.loads(success.content)["job_id"] == "J001"
    assert failure.is_error is True
    assert json.loads(failure.content)["code"] == ToolErrorCode.INVALID_ARGUMENTS
    assert [item.success for item in ledger.observations] == [True, False]


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("does_not_exist", {}, ToolErrorCode.UNKNOWN_TOOL),
        ("get_job", {"job_id": "missing"}, ToolErrorCode.NOT_FOUND),
        (
            "parse_job_requirements",
            {"jd_text": "Raw role description"},
            ToolErrorCode.PARSER_NOT_AVAILABLE,
        ),
        (
            "search_candidate_evidence",
            {
                "job_id": "J001",
                "job_version": 1,
                "requirement_id": "req-unknown",
                "candidate_id": "C001",
                "profile_version": 2,
                "query": "Python",
            },
            ToolErrorCode.INVALID_SCOPE,
        ),
    ],
)
async def test_toolbox_maps_expected_failures_to_stable_envelopes(
    jobintel_repo: SQLiteJobRepository,
    name: str,
    arguments: dict[str, object],
    expected: ToolErrorCode,
) -> None:
    ledger = ProvenanceLedger(f"run-{expected.value}")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)

    with pytest.raises(ToolExecutionError) as exc:
        await toolbox.execute(name, arguments, tool_call_id="call", iteration=1)

    assert exc.value.envelope.code is expected
    assert ledger.observations[0].error_code == expected.value
    assert ledger.observations[0].success is False


async def test_failed_search_scope_does_not_satisfy_guardrail(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger = ProvenanceLedger("run-failed-scope")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)
    arguments = {
        "job_id": "J001",
        "job_version": 1,
        "requirement_id": "req-unknown",
        "candidate_id": "C001",
        "profile_version": 2,
        "query": "Python",
    }
    with pytest.raises(ToolExecutionError):
        await toolbox.execute(
            "search_candidate_evidence", arguments, tool_call_id="search", iteration=1
        )

    scope = EvidenceSearchScope(
        **{key: value for key, value in arguments.items() if key != "query"}
    )
    assert ledger.observations[0].evidence_search_scope == scope
    assert ledger.has_successful_search(scope) is False


async def test_duplicate_call_id_is_rejected_without_second_observation(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger = ProvenanceLedger("run-duplicate")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)
    arguments = {"job_id": "J001"}
    await toolbox.execute("get_job", arguments, tool_call_id="same", iteration=1)

    with pytest.raises(ToolExecutionError) as exc:
        await toolbox.execute("get_job", arguments, tool_call_id="same", iteration=2)

    assert exc.value.envelope.code is ToolErrorCode.DUPLICATE_TOOL_CALL
    assert len(ledger.observations) == 1


async def test_terminal_guardrail_failure_is_structured_and_not_persisted(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    prepared_toolbox = JobIntelToolbox(jobintel_repo, ProvenanceLedger("prepare"))
    draft = await _researched_draft(prepared_toolbox)
    ledger = ProvenanceLedger("run-no-research")
    toolbox = JobIntelToolbox(jobintel_repo, ledger)

    with pytest.raises(ToolExecutionError) as exc:
        await toolbox.execute(
            "save_application_analysis",
            draft.model_dump(mode="json"),
            tool_call_id="save",
            iteration=1,
        )

    assert exc.value.envelope.code is ToolErrorCode.GUARDRAIL_REJECTED
    assert exc.value.envelope.field_path == "job_id"
    assert "violations" in exc.value.envelope.details
    assert ledger.observations[0].success is False


@pytest.mark.parametrize(
    ("error", "expected", "retryable"),
    [
        (
            IdempotencyConflictError("run id has a different payload"),
            ToolErrorCode.IDEMPOTENCY_CONFLICT,
            False,
        ),
        (
            PersistenceValidationError("analysis failed persistence validation"),
            ToolErrorCode.PERSISTENCE_REJECTED,
            True,
        ),
    ],
)
async def test_terminal_persistence_failures_have_stable_error_codes(
    jobintel_repo: SQLiteJobRepository,
    error: Exception,
    expected: ToolErrorCode,
    retryable: bool,
) -> None:
    draft = JobAnalysisDraft(
        job_id="J001",
        job_version=1,
        candidate_id="C001",
        profile_version=2,
        requirement_matches=(),
        next_action="Repair and retry.",
    )
    toolbox = JobIntelToolbox(
        jobintel_repo,
        ProvenanceLedger(f"run-{expected.value}"),
        analysis_service=_FailingAnalysisService(error),  # type: ignore[arg-type]
    )

    with pytest.raises(ToolExecutionError) as exc:
        await toolbox.execute(
            "save_application_analysis",
            draft.model_dump(mode="json"),
            tool_call_id="save",
            iteration=1,
        )

    assert exc.value.envelope.code is expected
    assert exc.value.envelope.retryable is retryable


async def test_fastmcp_uses_same_toolbox_and_structured_errors(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    ledger = ProvenanceLedger("run-mcp")
    server = build_server(JobIntelToolbox(jobintel_repo, ledger))

    async with Client(server) as client:
        result = await client.call_tool("get_job", {"job_id": "J001", "job_version": 1})
        with pytest.raises(ToolError) as exc:
            await client.call_tool("get_job", {"job_id": "missing"})

    assert result.data.job_id == "J001"
    assert ToolErrorCode.NOT_FOUND.value in str(exc.value)
    assert [item.success for item in ledger.observations] == [True, False]
