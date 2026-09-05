"""Provider, persistence, repair, and lifecycle tests for HR outreach."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import FakeProvider
from tests.jobintel.outreach_fixtures import build_outreach_scope, valid_outreach_message

from jobintel.errors import IdempotencyConflictError
from jobintel.outreach.models import OutreachStatus, OutreachTone
from jobintel.outreach.service import (
    OUTREACH_SUBMIT_TOOL,
    OutreachGenerationError,
    OutreachService,
)
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.providers.base import ToolCall, TurnResult, Usage

_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _turn(arguments: dict[str, object], call_id: str = "outreach") -> TurnResult:
    return TurnResult(
        tool_calls=[ToolCall(id=call_id, name=OUTREACH_SUBMIT_TOOL, arguments=arguments)],
        usage=Usage(input_tokens=20, output_tokens=10),
    )


async def test_generate_repairs_guardrail_failure_and_persists_citations(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, _profile, analysis = build_outreach_scope(jobintel_repo)
    jobintel_repo.save_analysis(analysis)
    valid = valid_outreach_message(job)
    invalid = valid.model_copy(update={"salutation": "王经理您好"})
    provider = FakeProvider(
        [_turn(invalid.model_dump(), "invalid"), _turn(valid.model_dump(), "valid")]
    )
    service = OutreachService(
        provider,
        jobintel_repo,
        max_repairs=1,
        clock=lambda: _NOW,
    )

    result = await service.generate(
        analysis_id=analysis.analysis_id,
        tone=OutreachTone.PROFESSIONAL,
        run_id="outreach-service-test",
    )

    assert result.telemetry.attempts == 2
    assert result.telemetry.repairs == 1
    assert result.telemetry.input_tokens == 40
    assert result.outreach.provider == "fake"
    assert result.outreach.status is OutreachStatus.DRAFT
    assert result.outreach.rendered_message.startswith("招聘负责人您好")
    persisted = jobintel_repo.get_outreach(result.outreach.outreach_id)
    assert persisted == result.outreach
    assert persisted.claims[0].requirement_ids == valid.claims[0].requirement_ids
    assert persisted.claims[0].evidence_ids == valid.claims[0].evidence_ids
    repair = provider.received_messages[1][-1].blocks[0]
    assert "invented_recruiter_name" in repair.content  # type: ignore[attr-defined]
    assert valid.claims[0].text not in repair.content  # type: ignore[attr-defined]


async def test_generate_exhausts_schema_repairs_without_persisting(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    _job, _profile, analysis = build_outreach_scope(jobintel_repo)
    jobintel_repo.save_analysis(analysis)
    provider = FakeProvider([TurnResult(text="没有工具调用")])

    with pytest.raises(OutreachGenerationError) as exc:
        await OutreachService(provider, jobintel_repo, max_repairs=0).generate(
            analysis_id=analysis.analysis_id
        )

    assert exc.value.telemetry.attempts == 1
    assert jobintel_repo.list_outreach() == ()


async def test_review_lifecycle_revision_and_stale_revision_protection(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    job, _profile, analysis = build_outreach_scope(jobintel_repo)
    jobintel_repo.save_analysis(analysis)
    provider = FakeProvider([_turn(valid_outreach_message(job).model_dump())])
    times = iter(_NOW + timedelta(minutes=index) for index in range(10))
    keys = iter(f"event-{index}" for index in range(10))
    service = OutreachService(
        provider,
        jobintel_repo,
        clock=lambda: next(times),
        event_key_factory=lambda: next(keys),
    )
    generated = await service.generate(analysis_id=analysis.analysis_id, run_id="lifecycle-test")
    outreach_id = generated.outreach.outreach_id

    approved = service.approve(outreach_id)
    copied = service.record_copied(outreach_id)
    opened = service.record_opened(outreach_id)
    assert approved.status is OutreachStatus.APPROVED
    assert copied.status is OutreachStatus.APPROVED
    assert opened.status is OutreachStatus.APPROVED

    revised = service.revise(outreach_id, "招聘负责人您好\n这是我人工调整后的完整沟通内容。")
    assert revised.revision == 2
    assert revised.status is OutreachStatus.DRAFT
    assert revised.is_user_edited is True
    assert revised.claims[0].claim_id != approved.claims[0].claim_id
    with pytest.raises(IdempotencyConflictError, match="stale"):
        service.confirm_sent(outreach_id, revision=1)

    service.approve(outreach_id, revision=2)
    sent = service.confirm_sent(outreach_id, revision=2)
    assert sent.status is OutreachStatus.SENT_CONFIRMED
    assert [item.event_type.value for item in jobintel_repo.list_outreach_events(outreach_id)] == [
        "approved",
        "copied",
        "opened",
        "approved",
        "sent_confirmed",
    ]


def test_generation_requires_provider(
    jobintel_repo: SQLiteJobRepository,
) -> None:
    with pytest.raises(RuntimeError, match="provider"):
        asyncio.run(OutreachService(None, jobintel_repo).generate(analysis_id="missing"))
