from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import FakeProvider

from jobintel.agent.tools import JobIntelToolbox, ToolExecutionError
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.provenance import ProvenanceLedger
from jobintel.providers.base import ToolCall, TurnResult, Usage
from jobintel.services.intake import AnalysisIntakeService, AnalysisRequest
from jobintel.services.jd_parser import (
    PARSER_PROMPT_VERSION,
    PARSER_SUBMIT_TOOL,
    PARSER_SYSTEM_PROMPT,
    JDParserError,
    JDParserService,
    raw_job_id,
    raw_source_sha256,
)

_NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
_RAW_JD = """Orbit Labs — Platform Engineer
Must have production Python experience.
Kubernetes experience is preferred.
"""


def test_parser_prompt_requires_simplified_chinese_requirements() -> None:
    assert PARSER_PROMPT_VERSION.endswith("zh-cn")
    assert "Requirement text must use concise Simplified Chinese" in PARSER_SYSTEM_PROMPT


def _submission() -> dict[str, object]:
    return {
        "company_name": "Orbit Labs",
        "title": "Platform Engineer",
        "location": "Remote",
        "employment_type": "Full-time",
        "requirements": [
            {
                "text": "Production Python experience",
                "category": "skill",
                "importance": "must",
                "normalized_skill": "Python",
            },
            {
                "text": "Kubernetes experience",
                "category": "skill",
                "importance": "preferred",
                "normalized_skill": "Kubernetes",
            },
        ],
    }


def _parsed_turn(call_id: str = "parsed") -> TurnResult:
    return TurnResult(
        tool_calls=[ToolCall(id=call_id, name=PARSER_SUBMIT_TOOL, arguments=_submission())],
        usage=Usage(input_tokens=20, output_tokens=10),
    )


async def test_parser_repairs_no_tool_turn_and_assigns_program_ids() -> None:
    provider = FakeProvider(
        [TurnResult(text="I will parse it.", usage=Usage(input_tokens=5)), _parsed_turn()]
    )
    parser = JDParserService(provider, max_repairs=1, clock=lambda: _NOW)

    result = await parser.parse(_RAW_JD, source_url="https://example.test/job")

    assert result.job.job_id == raw_job_id(_RAW_JD)
    assert result.job.source_sha256 == raw_source_sha256(_RAW_JD)
    assert result.job.created_at == _NOW
    assert result.job.source_url == "https://example.test/job"
    assert len({item.requirement_id for item in result.job.requirements}) == 2
    assert [item.source_order for item in result.job.requirements] == [0, 1]
    assert all(item.requirement_id.startswith("req_") for item in result.job.requirements)
    assert result.telemetry.attempts == 2
    assert result.telemetry.repairs == 1
    assert result.telemetry.prompt_version == PARSER_PROMPT_VERSION
    assert (result.telemetry.input_tokens, result.telemetry.output_tokens) == (25, 10)
    repair_messages = provider.received_messages[1]
    assert "submit_parsed_job exactly once" in repair_messages[-1].blocks[0].text  # type: ignore[attr-defined]


async def test_parser_schema_error_is_returned_as_safe_tool_result() -> None:
    invalid = TurnResult(
        tool_calls=[
            ToolCall(
                id="invalid",
                name=PARSER_SUBMIT_TOOL,
                arguments={"company_name": "Orbit", "requirements": []},
            )
        ]
    )
    provider = FakeProvider([invalid, _parsed_turn()])

    result = await JDParserService(provider, max_repairs=1).parse(_RAW_JD)

    assert result.telemetry.repairs == 1
    repair = provider.received_messages[1][-1].blocks[0]
    assert repair.is_error is True  # type: ignore[attr-defined]
    assert "INVALID_PARSED_JOB" in repair.content  # type: ignore[attr-defined]
    assert _RAW_JD not in repair.content  # type: ignore[attr-defined]


async def test_parser_rejects_mixed_or_wrong_calls_with_bounded_repairs() -> None:
    wrong = TurnResult(tool_calls=[ToolCall(id="wrong", name="other_tool", arguments={})])
    provider = FakeProvider([wrong, wrong])
    parser = JDParserService(provider, max_repairs=1)

    with pytest.raises(JDParserError) as exc:
        await parser.parse(_RAW_JD)

    assert exc.value.telemetry.attempts == 2
    assert exc.value.telemetry.repairs == 1
    assert provider.calls == 2
    assert "INVALID_PARSER_TURN" in provider.received_messages[1][-1].blocks[0].content  # type: ignore[attr-defined]


def test_parser_validates_budget_and_raw_identity_input() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        JDParserService(FakeProvider([]), max_repairs=-1)
    with pytest.raises(ValueError, match="must not be empty"):
        raw_job_id("  ")
    with pytest.raises(ValueError, match="must not be empty"):
        raw_source_sha256("\n")
    assert raw_job_id("A  \nB") == raw_job_id("A\nB")
    assert raw_source_sha256("A  \nB") == raw_source_sha256("A\nB")


async def test_intake_resolves_stored_and_raw_scopes_without_writing(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    stored = await AnalysisIntakeService(jobintel_repo).resolve(
        AnalysisRequest(run_id="stored", candidate_id="C001", job_id="J001")
    )
    parser = JDParserService(FakeProvider([_parsed_turn()]), clock=lambda: _NOW)
    raw = await AnalysisIntakeService(jobintel_repo, parser).resolve(
        AnalysisRequest(
            run_id="raw",
            candidate_id="C001",
            jd_text=_RAW_JD,
            jd_source_url="https://example.test/job",
        )
    )

    assert stored.job.job_id == "J001"
    assert stored.profile_version == 2
    assert stored.is_raw_job is False
    assert raw.job.job_id == raw_job_id(_RAW_JD)
    assert raw.is_raw_job is True
    assert raw.parser_telemetry is not None
    with pytest.raises(Exception, match="job not found"):
        jobintel_repo.get_job(raw.job.job_id, 1)


async def test_raw_intake_requires_parser_and_request_can_generate_run_id(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    request = AnalysisRequest(candidate_id="C001", jd_text=_RAW_JD)
    assert request.run_id.startswith("run_")

    with pytest.raises(RuntimeError, match="requires a JDParserService"):
        await AnalysisIntakeService(jobintel_repo).resolve(request)


async def test_parse_tool_supports_raw_jd_when_parser_is_injected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    parser = JDParserService(FakeProvider([_parsed_turn()]), clock=lambda: _NOW)
    toolbox = JobIntelToolbox(
        jobintel_repo,
        ProvenanceLedger("raw-tool"),
        jd_parser=parser,
    )

    parsed = await toolbox.execute(
        "parse_job_requirements",
        {"jd_text": _RAW_JD},
        tool_call_id="parse",
        iteration=1,
    )
    staged = await toolbox.execute(
        "get_job",
        {"job_id": raw_job_id(_RAW_JD), "job_version": 1},
        tool_call_id="get",
        iteration=2,
    )

    assert parsed.job_id == staged.job_id  # type: ignore[attr-defined]
    assert len(parsed.requirements) == 2  # type: ignore[attr-defined]


async def test_parse_tool_maps_parser_repair_exhaustion(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    parser = JDParserService(FakeProvider([TurnResult(text="no output")]), max_repairs=0)
    ledger = ProvenanceLedger("raw-tool-failure")
    toolbox = JobIntelToolbox(jobintel_repo, ledger, jd_parser=parser)

    with pytest.raises(ToolExecutionError) as exc:
        await toolbox.execute(
            "parse_job_requirements",
            {"jd_text": _RAW_JD},
            tool_call_id="parse",
            iteration=1,
        )

    assert exc.value.envelope.code.value == "PARSER_REPAIR_LIMIT"
    assert exc.value.envelope.details == {"attempts": 1, "repairs": 0}
    assert ledger.observations[0].error_code == "PARSER_REPAIR_LIMIT"


@pytest.mark.parametrize(
    "payload",
    [
        {"run_id": "x", "candidate_id": "C001"},
        {
            "run_id": "x",
            "candidate_id": "C001",
            "job_id": "J001",
            "jd_text": "raw",
        },
        {
            "run_id": "x",
            "candidate_id": "C001",
            "jd_text": "raw",
            "job_version": 1,
        },
        {
            "run_id": "x",
            "candidate_id": "C001",
            "job_id": "J001",
            "jd_source_url": "https://example.test",
        },
    ],
)
def test_analysis_request_rejects_ambiguous_sources(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AnalysisRequest.model_validate(payload)
