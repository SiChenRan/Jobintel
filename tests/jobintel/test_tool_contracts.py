from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobintel.agent.tools import JobIntelToolbox
from jobintel.mcp_server import build_server
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.provenance import ProvenanceLedger
from jobintel.tool_contracts import (
    TOOL_CONTRACT_BY_NAME,
    TOOL_CONTRACTS,
    CandidateProfileSummary,
    ParseJobRequirementsRequest,
    ToolEffect,
)


def test_canonical_contracts_define_exactly_one_terminal_write() -> None:
    assert [contract.name for contract in TOOL_CONTRACTS] == [
        "get_job",
        "parse_job_requirements",
        "get_candidate_profile",
        "search_candidate_evidence",
        "get_company",
        "save_application_analysis",
    ]
    assert len(TOOL_CONTRACT_BY_NAME) == 6
    terminal = [contract for contract in TOOL_CONTRACTS if contract.is_terminal]
    assert [(contract.name, contract.effect) for contract in terminal] == [
        ("save_application_analysis", ToolEffect.WRITE)
    ]
    terminal_properties = terminal[0].input_schema()["properties"]
    assert {
        "score",
        "recommendation",
        "analysis_id",
        "run_id",
        "created_at",
    }.isdisjoint(terminal_properties)


def test_provider_specs_are_direct_contract_projections() -> None:
    specs = JobIntelToolbox.specs()

    assert len(specs) == len(TOOL_CONTRACTS)
    for contract, spec in zip(TOOL_CONTRACTS, specs, strict=True):
        assert spec.name == contract.name
        assert spec.description == contract.description
        assert spec.input_schema == contract.input_schema()
        assert "title" not in spec.input_schema


async def test_fastmcp_schema_and_effect_annotations_match_contracts(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    server = build_server(JobIntelToolbox(jobintel_repo, ProvenanceLedger("run-mcp-schema")))
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == set(TOOL_CONTRACT_BY_NAME)
    for name, contract in TOOL_CONTRACT_BY_NAME.items():
        tool = tools[name]
        assert tool.description == contract.description
        assert tool.parameters == contract.input_schema()
        assert tool.output_schema == contract.output_schema()
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is (contract.effect is not ToolEffect.WRITE)
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"job_id": "J001", "jd_text": "raw"},
        {"jd_text": "raw", "job_version": 1},
    ],
)
def test_parse_request_requires_one_valid_source(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ParseJobRequirementsRequest.model_validate(payload)


def test_candidate_summary_never_exposes_evidence_content(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    profile = jobintel_repo.get_candidate_profile("C001", 2)
    summary = CandidateProfileSummary.from_profile(profile)
    serialized = summary.model_dump_json()

    assert summary.evidence_count == len(profile.evidence)
    assert summary.skills == tuple(sorted(summary.skills, key=str.casefold))
    assert all(evidence.content not in serialized for evidence in profile.evidence)
