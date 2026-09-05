from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from jobintel.agent.core import (
    AgentFailureCode,
    AgentState,
    JobIntelAgent,
    JobIntelAgentError,
)
from jobintel.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from jobintel.config import JobIntelSettings
from jobintel.errors import EntityNotFoundError
from jobintel.models import JobAnalysisDraft, MatchStatus, RequirementMatchDraft
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.providers.base import ToolCall, TurnResult, Usage
from jobintel.services.analysis import stable_analysis_id
from jobintel.services.intake import AnalysisRequest
from jobintel.services.jd_parser import PARSER_PROMPT_VERSION, PARSER_SUBMIT_TOOL, raw_job_id
from jobintel.tool_contracts import ToolErrorCode

pytestmark = pytest.mark.asyncio

_USAGE = Usage(input_tokens=10, output_tokens=5)
_EVIDENCE_IDS = (
    "ev-python-api",
    "ev-python-api",
    "ev-python-api",
    "ev-atlas-platform",
)
_RAW_JD = """Orbit Labs Platform Engineer
Must have production Python experience.
Kubernetes deployment experience is preferred.
"""


async def test_analysis_prompt_requires_simplified_chinese_user_content() -> None:
    assert PROMPT_VERSION.endswith("zh-cn")
    assert "MUST use Simplified Chinese" in SYSTEM_PROMPT
    assert "requirement match reason" in SYSTEM_PROMPT


def _stored_scope_turn(
    job_id: str = "J001",
    *,
    include_company: bool = False,
    profile_first: bool = False,
) -> TurnResult:
    calls = [
        ToolCall(
            id="get-job",
            name="get_job",
            arguments={"job_id": job_id, "job_version": 1},
        ),
        ToolCall(
            id="get-profile",
            name="get_candidate_profile",
            arguments={"candidate_id": "C001", "profile_version": 2},
        ),
    ]
    if profile_first:
        calls.reverse()
    if include_company:
        calls.append(
            ToolCall(
                id="get-company",
                name="get_company",
                arguments={"company_id": "co-orbit"},
            )
        )
    return TurnResult(tool_calls=calls, usage=_USAGE)


def _stored_search_turn(repo: SQLiteJobRepository, *, reverse: bool = False) -> TurnResult:
    job = repo.get_job("J001", 1)
    requirements = list(job.requirements)
    if reverse:
        requirements.reverse()
    return TurnResult(
        tool_calls=[
            ToolCall(
                id=f"search-{requirement.source_order}",
                name="search_candidate_evidence",
                arguments={
                    "job_id": job.job_id,
                    "job_version": job.job_version,
                    "requirement_id": requirement.requirement_id,
                    "candidate_id": "C001",
                    "profile_version": 2,
                    "query": requirement.normalized_skill or requirement.text,
                },
            )
            for requirement in requirements
        ],
        usage=_USAGE,
    )


def _stored_draft(repo: SQLiteJobRepository) -> JobAnalysisDraft:
    job = repo.get_job("J001", 1)
    return JobAnalysisDraft(
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id="C001",
        profile_version=2,
        requirement_matches=tuple(
            RequirementMatchDraft(
                requirement_id=requirement.requirement_id,
                status=MatchStatus.MATCHED,
                evidence_ids=(evidence_id,),
                confidence=0.9,
                reason="Scoped search returned supporting evidence.",
            )
            for requirement, evidence_id in zip(job.requirements, _EVIDENCE_IDS, strict=True)
        ),
        next_action="Tailor the resume and apply.",
    )


def _submit(draft: JobAnalysisDraft, call_id: str = "save") -> TurnResult:
    return TurnResult(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="save_application_analysis",
                arguments=draft.model_dump(mode="json"),
            )
        ],
        usage=_USAGE,
    )


def _settings(**updates: int) -> JobIntelSettings:
    return JobIntelSettings(
        agent_max_iterations=updates.get("agent_max_iterations", 8),
        agent_max_repairs=updates.get("agent_max_repairs", 2),
        agent_max_tool_calls=updates.get("agent_max_tool_calls", 30),
        parser_max_repairs=updates.get("parser_max_repairs", 1),
    )


async def test_stored_job_e2e_supports_optional_lookup_and_structured_telemetry(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    draft = _stored_draft(jobintel_repo)
    provider = FakeProvider(
        [
            _stored_scope_turn(include_company=True, profile_first=True),
            _stored_search_turn(jobintel_repo, reverse=True),
            _submit(draft),
        ]
    )
    agent = JobIntelAgent(provider, jobintel_repo, _settings())

    result = await agent.analyze(
        AnalysisRequest(run_id="run-agent-stored", candidate_id="C001", job_id="J001")
    )

    assert result.analysis.score == 100
    assert result.analysis.prompt_version == PROMPT_VERSION
    assert result.analysis.run_id == "run-agent-stored"
    assert result.telemetry.iterations == 3
    assert result.telemetry.repairs == 0
    assert result.telemetry.input_tokens == 30
    assert result.telemetry.output_tokens == 15
    assert result.telemetry.tool_calls[-1] == "save_application_analysis"
    states = [event.state for event in result.telemetry.trace]
    assert states[0] is AgentState.INITIALIZE
    assert states[-1] is AgentState.COMPLETE
    assert AgentState.EXECUTE_TOOLS in states
    assert jobintel_repo.get_analysis(result.analysis.analysis_id) == result.analysis


async def test_agent_accepts_search_before_identity_reads(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    provider = FakeProvider(
        [
            _stored_search_turn(jobintel_repo),
            _stored_scope_turn(profile_first=True),
            _submit(_stored_draft(jobintel_repo)),
        ]
    )

    result = await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
        AnalysisRequest(run_id="run-order", candidate_id="C001", job_id="J001")
    )

    assert result.analysis.score == 100
    assert result.telemetry.tool_calls[0] == "search_candidate_evidence"


async def test_no_tool_nudge_and_unknown_tool_recovery(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    provider = FakeProvider(
        [
            TurnResult(text="I need more context.", usage=_USAGE),
            TurnResult(
                tool_calls=[ToolCall(id="unknown", name="invented_tool", arguments={})],
                usage=_USAGE,
            ),
            _stored_scope_turn(),
            _stored_search_turn(jobintel_repo),
            _submit(_stored_draft(jobintel_repo)),
        ]
    )

    result = await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
        AnalysisRequest(run_id="run-recovery", candidate_id="C001", job_id="J001")
    )

    assert result.telemetry.iterations == 5
    assert "invented_tool" in result.telemetry.tool_calls
    assert AgentState.NO_TOOL_NUDGE in {event.state for event in result.telemetry.trace}
    assert "Completion requires" in provider.received_messages[1][-1].blocks[0].text  # type: ignore[attr-defined]
    assert ToolErrorCode.UNKNOWN_TOOL.value in provider.received_messages[2][-1].blocks[0].content  # type: ignore[attr-defined]


async def test_mixed_terminal_turn_is_rejected_then_repaired(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    draft = _stored_draft(jobintel_repo)
    mixed = TurnResult(
        tool_calls=[
            ToolCall(
                id="mixed-save",
                name="save_application_analysis",
                arguments=draft.model_dump(mode="json"),
            ),
            ToolCall(
                id="mixed-read",
                name="get_company",
                arguments={"company_id": "co-orbit"},
            ),
        ],
        usage=_USAGE,
    )
    provider = FakeProvider(
        [
            _stored_scope_turn(),
            _stored_search_turn(jobintel_repo),
            mixed,
            _submit(draft, "repaired-save"),
        ]
    )

    result = await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
        AnalysisRequest(run_id="run-mixed", candidate_id="C001", job_id="J001")
    )

    assert result.telemetry.repairs == 1
    repair_blocks = provider.received_messages[3][-1].blocks
    assert len(repair_blocks) == 2
    assert all(block.is_error for block in repair_blocks)  # type: ignore[union-attr]
    assert all(
        ToolErrorCode.INVALID_TERMINAL_TURN.value in block.content  # type: ignore[union-attr]
        for block in repair_blocks
    )


async def test_mixed_terminal_turn_obeys_zero_repair_budget(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    draft = _stored_draft(jobintel_repo)
    mixed = TurnResult(
        tool_calls=[
            ToolCall(
                id="mixed-save-limit",
                name="save_application_analysis",
                arguments=draft.model_dump(mode="json"),
            ),
            ToolCall(id="mixed-read-limit", name="get_job", arguments={"job_id": "J001"}),
        ]
    )
    provider = FakeProvider([_stored_scope_turn(), _stored_search_turn(jobintel_repo), mixed])

    with pytest.raises(JobIntelAgentError) as exc:
        await JobIntelAgent(provider, jobintel_repo, _settings(agent_max_repairs=0)).analyze(
            AnalysisRequest(run_id="run-mixed-limit", candidate_id="C001", job_id="J001")
        )

    assert exc.value.code is AgentFailureCode.REPAIR_LIMIT


async def test_guardrail_repair_and_model_controlled_score_are_rejected(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    good = _stored_draft(jobintel_repo)
    wrong_first = good.requirement_matches[0].model_copy(
        update={"evidence_ids": ("ev-atlas-platform",)}
    )
    bad_scope = good.model_copy(
        update={"requirement_matches": (wrong_first, *good.requirement_matches[1:])}
    )
    score_payload = good.model_dump(mode="json")
    score_payload["score"] = 1
    score_payload["recommendation"] = "skip"
    provider = FakeProvider(
        [
            _stored_scope_turn(),
            _stored_search_turn(jobintel_repo),
            _submit(bad_scope, "bad-scope"),
            TurnResult(
                tool_calls=[
                    ToolCall(
                        id="bad-score",
                        name="save_application_analysis",
                        arguments=score_payload,
                    )
                ],
                usage=_USAGE,
            ),
            _submit(good, "good-save"),
        ]
    )

    result = await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
        AnalysisRequest(run_id="run-repairs", candidate_id="C001", job_id="J001")
    )

    assert result.telemetry.repairs == 2
    assert result.analysis.score == 100
    assert result.analysis.recommendation.value == "strong_apply"
    assert "GUARDRAIL_REJECTED" in provider.received_messages[3][-1].blocks[0].content  # type: ignore[attr-defined]
    assert "INVALID_ARGUMENTS" in provider.received_messages[4][-1].blocks[0].content  # type: ignore[attr-defined]


async def test_terminal_repair_limit_fails_without_analysis_write(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    good = _stored_draft(jobintel_repo)
    forged = good.requirement_matches[0].model_copy(update={"evidence_ids": ("ev-forged",)})
    bad = good.model_copy(update={"requirement_matches": (forged, *good.requirement_matches[1:])})
    provider = FakeProvider(
        [
            _stored_scope_turn(),
            _stored_search_turn(jobintel_repo),
            _submit(bad),
        ]
    )

    with pytest.raises(JobIntelAgentError) as exc:
        await JobIntelAgent(provider, jobintel_repo, _settings(agent_max_repairs=0)).analyze(
            AnalysisRequest(run_id="run-repair-limit", candidate_id="C001", job_id="J001")
        )

    assert exc.value.code is AgentFailureCode.REPAIR_LIMIT
    assert exc.value.telemetry is not None
    assert exc.value.telemetry.trace[-1].state is AgentState.FAILED
    with pytest.raises(EntityNotFoundError):
        jobintel_repo.get_analysis(stable_analysis_id("run-repair-limit"))


async def test_non_retryable_terminal_error_fails_immediately(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    draft = _stored_draft(jobintel_repo)
    provider = FakeProvider(
        [
            _stored_scope_turn(),
            _stored_search_turn(jobintel_repo),
            _submit(draft, "get-job"),
        ]
    )

    with pytest.raises(JobIntelAgentError) as exc:
        await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
            AnalysisRequest(run_id="run-terminal-failed", candidate_id="C001", job_id="J001")
        )

    assert exc.value.code is AgentFailureCode.TERMINAL_FAILED
    assert exc.value.telemetry is not None
    assert exc.value.telemetry.repairs == 0


async def test_iteration_and_tool_call_budgets_are_bounded(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    idle = FakeProvider([TurnResult(text="thinking"), TurnResult(text="still thinking")])
    with pytest.raises(JobIntelAgentError) as iteration_exc:
        await JobIntelAgent(idle, jobintel_repo, _settings(agent_max_iterations=2)).analyze(
            AnalysisRequest(run_id="run-iterations", candidate_id="C001", job_id="J001")
        )
    assert iteration_exc.value.code is AgentFailureCode.ITERATION_LIMIT
    assert idle.calls == 2

    too_many = FakeProvider([_stored_scope_turn()])
    with pytest.raises(JobIntelAgentError) as tool_exc:
        await JobIntelAgent(too_many, jobintel_repo, _settings(agent_max_tool_calls=1)).analyze(
            AnalysisRequest(run_id="run-tools", candidate_id="C001", job_id="J001")
        )
    assert tool_exc.value.code is AgentFailureCode.TOOL_CALL_LIMIT
    assert tool_exc.value.telemetry is not None
    assert tool_exc.value.telemetry.tool_calls == ("get_job", "get_candidate_profile")


async def test_intake_and_parser_failures_are_structured(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    provider = FakeProvider([])
    with pytest.raises(JobIntelAgentError) as intake_exc:
        await JobIntelAgent(provider, jobintel_repo, _settings()).analyze(
            AnalysisRequest(run_id="missing", candidate_id="missing", job_id="J001")
        )
    assert intake_exc.value.code is AgentFailureCode.INTAKE_FAILED
    assert provider.calls == 0

    parser_provider = FakeProvider([TurnResult(text="no"), TurnResult(text="still no")])
    with pytest.raises(JobIntelAgentError) as parser_exc:
        await JobIntelAgent(
            FakeProvider([]),
            jobintel_repo,
            _settings(parser_max_repairs=1),
            parser_provider=parser_provider,
        ).analyze(AnalysisRequest(run_id="parse-fail", candidate_id="C001", jd_text=_RAW_JD))
    assert parser_exc.value.code is AgentFailureCode.PARSER_REPAIR_LIMIT
    assert parser_exc.value.telemetry is not None
    assert parser_exc.value.telemetry.parser is not None
    assert parser_exc.value.telemetry.parser.attempts == 2


def _raw_parser_turn() -> TurnResult:
    return TurnResult(
        tool_calls=[
            ToolCall(
                id="parsed-raw",
                name=PARSER_SUBMIT_TOOL,
                arguments={
                    "company_name": "Orbit Labs",
                    "title": "Platform Engineer",
                    "requirements": [
                        {
                            "text": "Production Python experience",
                            "category": "skill",
                            "importance": "must",
                            "normalized_skill": "Python",
                        },
                        {
                            "text": "Kubernetes deployment experience",
                            "category": "skill",
                            "importance": "preferred",
                            "normalized_skill": "Kubernetes",
                        },
                    ],
                },
            )
        ],
        usage=Usage(input_tokens=20, output_tokens=10),
    )


def _raw_agent_turns(jobintel_repo: SQLiteJobRepository) -> list[TurnResult]:
    job_id = raw_job_id(_RAW_JD)
    from jobintel.models import RequirementCategory, stable_requirement_id

    requirement_specs = (
        ("Production Python experience", "Python", "ev-python-api"),
        ("Kubernetes deployment experience", "Kubernetes", "ev-atlas-platform"),
    )
    requirement_ids = [
        stable_requirement_id(
            job_id=job_id,
            job_version=1,
            text=text,
            category=RequirementCategory.SKILL,
        )
        for text, _query, _evidence_id in requirement_specs
    ]
    scope = TurnResult(
        tool_calls=[
            ToolCall(
                id="raw-job",
                name="get_job",
                arguments={"job_id": job_id, "job_version": 1},
            ),
            ToolCall(
                id="raw-profile",
                name="get_candidate_profile",
                arguments={"candidate_id": "C001", "profile_version": 2},
            ),
        ],
        usage=_USAGE,
    )
    searches = TurnResult(
        tool_calls=[
            ToolCall(
                id=f"raw-search-{index}",
                name="search_candidate_evidence",
                arguments={
                    "job_id": job_id,
                    "job_version": 1,
                    "requirement_id": requirement_id,
                    "candidate_id": "C001",
                    "profile_version": 2,
                    "query": query,
                },
            )
            for index, (requirement_id, (_text, query, _evidence_id)) in enumerate(
                zip(requirement_ids, requirement_specs, strict=True)
            )
        ],
        usage=_USAGE,
    )
    draft = JobAnalysisDraft(
        job_id=job_id,
        job_version=1,
        candidate_id="C001",
        profile_version=2,
        requirement_matches=tuple(
            RequirementMatchDraft(
                requirement_id=requirement_id,
                status=MatchStatus.MATCHED,
                evidence_ids=(evidence_id,),
                confidence=0.9,
                reason="Raw job requirement has scoped candidate evidence.",
            )
            for requirement_id, (_text, _query, evidence_id) in zip(
                requirement_ids, requirement_specs, strict=True
            )
        ),
        next_action="Apply to the role.",
    )
    return [scope, searches, _submit(draft, "raw-save")]


async def test_raw_jd_e2e_atomically_persists_staged_job_and_analysis(
    jobintel_db: JobIntelDatabase,
    jobintel_repo: SQLiteJobRepository,
) -> None:
    parser_provider = FakeProvider([_raw_parser_turn()])
    agent_provider = FakeProvider(_raw_agent_turns(jobintel_repo))
    agent = JobIntelAgent(
        agent_provider,
        jobintel_repo,
        _settings(),
        parser_provider=parser_provider,
    )

    result = await agent.analyze(
        AnalysisRequest(run_id="run-raw", candidate_id="C001", jd_text=_RAW_JD)
    )

    assert result.analysis.job_id == raw_job_id(_RAW_JD)
    assert result.analysis.parser_version == PARSER_PROMPT_VERSION
    assert result.telemetry.parser is not None
    assert result.telemetry.input_tokens == 50
    assert result.telemetry.output_tokens == 25
    persisted = jobintel_repo.get_job(raw_job_id(_RAW_JD), 1)
    assert len(persisted.requirements) == 2
    assert jobintel_repo.get_analysis(result.analysis.analysis_id) == result.analysis
    assert jobintel_db.connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_raw_jd_agent_failure_leaves_no_staged_job_rows(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    turns = _raw_agent_turns(jobintel_repo)[:2]
    turns.append(
        TurnResult(
            tool_calls=[
                ToolCall(
                    id="invalid-raw-save",
                    name="save_application_analysis",
                    arguments={},
                )
            ]
        )
    )
    agent = JobIntelAgent(
        FakeProvider(turns),
        jobintel_repo,
        _settings(agent_max_repairs=0),
        parser_provider=FakeProvider([_raw_parser_turn()]),
    )

    with pytest.raises(JobIntelAgentError) as exc:
        await agent.analyze(
            AnalysisRequest(run_id="run-raw-fail", candidate_id="C001", jd_text=_RAW_JD)
        )

    assert exc.value.code is AgentFailureCode.REPAIR_LIMIT
    with pytest.raises(EntityNotFoundError):
        jobintel_repo.get_job(raw_job_id(_RAW_JD), 1)
