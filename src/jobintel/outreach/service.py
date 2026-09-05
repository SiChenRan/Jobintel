"""Provider orchestration and reviewed lifecycle for HR outreach drafts."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import Field, ValidationError

from jobintel.errors import IdempotencyConflictError
from jobintel.models import FrozenDomainModel, NonEmptyStr
from jobintel.outreach.finalizer import finalize_outreach
from jobintel.outreach.guardrail import OutreachGuardrailViolation, validate_outreach_message
from jobintel.outreach.models import (
    OutreachClaim,
    OutreachDraft,
    OutreachEvent,
    OutreachEventType,
    OutreachMessageDraft,
    OutreachStatus,
    OutreachTone,
    stable_outreach_claim_id,
    stable_outreach_event_id,
)
from jobintel.outreach.policy import BOSS_DRAFT_POLICY
from jobintel.outreach.prompts import build_outreach_prompt
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.providers.base import LLMProvider, Message, ToolCall, ToolResultBlock, ToolSpec, Usage

OUTREACH_SUBMIT_TOOL = "submit_outreach_draft"


class OutreachTelemetry(FrozenDomainModel):
    """Provider and repair accounting for one generation attempt."""

    provider: NonEmptyStr
    prompt_version: NonEmptyStr
    attempts: int = Field(ge=1)
    repairs: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class OutreachGenerationResult(FrozenDomainModel):
    """Persisted draft plus generation telemetry."""

    outreach: OutreachDraft
    telemetry: OutreachTelemetry


class OutreachGenerationError(RuntimeError):
    """Raised after bounded structured-output repairs are exhausted."""

    def __init__(
        self,
        telemetry: OutreachTelemetry,
        violations: tuple[OutreachGuardrailViolation, ...] = (),
    ) -> None:
        """Retain failure telemetry and the final deterministic violations."""
        super().__init__(f"outreach generation failed after {telemetry.attempts} attempt(s)")
        self.telemetry = telemetry
        self.violations = violations


class OutreachService:
    """Generate evidence-grounded drafts and record explicit user review actions."""

    def __init__(
        self,
        provider: LLMProvider | None,
        repository: SQLiteJobRepository,
        *,
        max_repairs: int = 2,
        clock: Callable[[], datetime] | None = None,
        event_key_factory: Callable[[], str] | None = None,
    ) -> None:
        """Configure provider, repository, repair budget, clock, and event keys."""
        if max_repairs < 0:
            raise ValueError("max_repairs must be non-negative")
        self._provider = provider
        self._repository = repository
        self._max_repairs = max_repairs
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_key_factory = event_key_factory or (lambda: uuid4().hex)

    async def generate(
        self,
        *,
        analysis_id: str,
        tone: OutreachTone = OutreachTone.PROFESSIONAL,
        focus_requirement_ids: tuple[str, ...] = (),
        run_id: str | None = None,
    ) -> OutreachGenerationResult:
        """Generate, repair, guard, finalize, and persist one initial draft."""
        if self._provider is None:
            raise RuntimeError("an LLM provider is required to generate outreach")
        analysis = self._repository.get_analysis(analysis_id)
        job = self._repository.get_job(analysis.job_id, analysis.job_version)
        profile = self._repository.get_candidate_profile(
            analysis.candidate_id, analysis.profile_version
        )
        recruiter_name = self._repository.find_recruiter_name_for_source_url(job.source_url)
        prompt = build_outreach_prompt(
            analysis=analysis,
            job=job,
            profile=profile,
            tone=tone,
            recruiter_name=recruiter_name,
            focus_requirement_ids=focus_requirement_ids,
            max_claims=BOSS_DRAFT_POLICY.max_claims,
        )
        messages = [Message.user_text(prompt.user)]
        spec = self._tool_spec()
        usage = Usage()
        last_violations: tuple[OutreachGuardrailViolation, ...] = ()
        for attempt in range(1, self._max_repairs + 2):
            turn = await self._provider.run_turn(prompt.system, messages, [spec])
            usage = usage + turn.usage
            messages.append(turn.assistant_message())
            submission, error_results = self._submission(turn.tool_calls)
            if submission is not None:
                guardrail = validate_outreach_message(
                    draft=submission,
                    analysis=analysis,
                    job=job,
                    profile=profile,
                    recruiter_name=recruiter_name,
                    policy=BOSS_DRAFT_POLICY,
                )
                last_violations = guardrail.violations
                if guardrail.is_valid:
                    outreach = finalize_outreach(
                        submission=submission,
                        analysis=analysis,
                        job=job,
                        profile=profile,
                        tone=tone,
                        provider=self._provider.name,
                        prompt_version=prompt.prompt_version,
                        run_id=run_id or uuid4().hex,
                        created_at=self._clock(),
                    )
                    persisted = self._repository.save_outreach(outreach)
                    return OutreachGenerationResult(
                        outreach=persisted,
                        telemetry=self._telemetry(prompt.prompt_version, attempt, usage),
                    )
                error_results = [
                    ToolResultBlock(
                        tool_call_id=turn.tool_calls[0].id,
                        content=json.dumps(
                            {
                                "code": "OUTREACH_GUARDRAIL_REJECTED",
                                "message": "draft failed deterministic validation",
                                "retryable": True,
                                "violations": [
                                    {
                                        "code": item.code.value,
                                        "field_path": item.field_path,
                                        "requirement_id": item.requirement_id,
                                        "evidence_id": item.evidence_id,
                                    }
                                    for item in guardrail.violations
                                ],
                            },
                            separators=(",", ":"),
                        ),
                        is_error=True,
                    )
                ]
            if attempt <= self._max_repairs:
                messages.append(
                    Message(role="user", blocks=error_results)
                    if error_results
                    else Message.user_text(
                        "Output was missing. Call submit_outreach_draft exactly once "
                        "with arguments matching its schema."
                    )
                )
        raise OutreachGenerationError(
            self._telemetry(prompt.prompt_version, self._max_repairs + 1, usage),
            last_violations,
        )

    @staticmethod
    def _tool_spec() -> ToolSpec:
        schema = OutreachMessageDraft.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(
            name=OUTREACH_SUBMIT_TOOL,
            description="提交一份基于已验证证据的中文首次联系草稿。",
            input_schema=schema,
        )

    @staticmethod
    def _submission(
        calls: Sequence[ToolCall],
    ) -> tuple[OutreachMessageDraft | None, list[ToolResultBlock]]:
        """Accept exactly one valid terminal call or return safe repair blocks."""
        if len(calls) == 1 and calls[0].name == OUTREACH_SUBMIT_TOOL:
            call = calls[0]
            try:
                return OutreachMessageDraft.model_validate(call.arguments), []
            except ValidationError as exc:
                first = exc.errors(include_input=False)[0]
                field_path = ".".join(str(item) for item in first["loc"])
                return None, [
                    ToolResultBlock(
                        tool_call_id=call.id,
                        content=json.dumps(
                            {
                                "code": "INVALID_OUTREACH_DRAFT",
                                "message": "draft failed schema validation",
                                "retryable": True,
                                "field_path": field_path,
                            },
                            separators=(",", ":"),
                        ),
                        is_error=True,
                    )
                ]
        return None, [
            ToolResultBlock(
                tool_call_id=call.id,
                content=(
                    '{"code":"INVALID_OUTREACH_TURN","message":"call '
                    'submit_outreach_draft exactly once","retryable":true}'
                ),
                is_error=True,
            )
            for call in calls
        ]

    def revise(
        self, outreach_id: str, message: str, *, revision: int | None = None
    ) -> OutreachDraft:
        """Create a new draft revision carrying explicit user-authored text."""
        current = self._repository.get_outreach(outreach_id)
        if revision is not None and current.revision != revision:
            raise IdempotencyConflictError(f"outreach revision is stale: {outreach_id}@{revision}")
        if current.status in (OutreachStatus.SENT_CONFIRMED, OutreachStatus.DISMISSED):
            raise ValueError("terminal outreach cannot be revised")
        edited = message.strip()
        if not edited:
            raise ValueError("revised outreach message must not be empty")
        if len(edited) > BOSS_DRAFT_POLICY.max_message_chars:
            raise ValueError(
                f"revised outreach exceeds {BOSS_DRAFT_POLICY.max_message_chars} characters"
            )
        revision = current.revision + 1
        now = self._clock()
        claims = tuple(
            OutreachClaim(
                **claim.model_dump(exclude={"claim_id", "source_order"}),
                claim_id=stable_outreach_claim_id(
                    outreach_id=outreach_id,
                    revision=revision,
                    source_order=order,
                ),
                source_order=order,
            )
            for order, claim in enumerate(current.claims)
        )
        revised = current.model_copy(
            update={
                "revision": revision,
                "claims": claims,
                "user_edited_message": edited,
                "status": OutreachStatus.DRAFT,
                "created_at": now,
                "updated_at": now,
            }
        )
        return self._repository.save_outreach(revised)

    def approve(self, outreach_id: str, *, revision: int | None = None) -> OutreachDraft:
        """Approve the latest draft revision."""
        return self._record(outreach_id, OutreachEventType.APPROVED, revision=revision)

    def record_copied(self, outreach_id: str, *, revision: int | None = None) -> OutreachDraft:
        """Record copying of an approved draft without sending it."""
        return self._record(outreach_id, OutreachEventType.COPIED, revision=revision)

    def record_opened(self, outreach_id: str, *, revision: int | None = None) -> OutreachDraft:
        """Record opening the source job without automating platform interaction."""
        return self._record(outreach_id, OutreachEventType.OPENED, revision=revision)

    def confirm_sent(self, outreach_id: str, *, revision: int | None = None) -> OutreachDraft:
        """Record the user's confirmation that they manually sent the message."""
        return self._record(outreach_id, OutreachEventType.SENT_CONFIRMED, revision=revision)

    def dismiss(self, outreach_id: str, *, revision: int | None = None) -> OutreachDraft:
        """Dismiss a draft revision without contacting the platform."""
        return self._record(outreach_id, OutreachEventType.DISMISSED, revision=revision)

    def _record(
        self,
        outreach_id: str,
        event_type: OutreachEventType,
        *,
        revision: int | None,
    ) -> OutreachDraft:
        draft = self._repository.get_outreach(outreach_id, revision)
        target = {
            OutreachEventType.APPROVED: OutreachStatus.APPROVED,
            OutreachEventType.SENT_CONFIRMED: OutreachStatus.SENT_CONFIRMED,
            OutreachEventType.DISMISSED: OutreachStatus.DISMISSED,
            OutreachEventType.COPIED: draft.status,
            OutreachEventType.OPENED: draft.status,
        }[event_type]
        event = OutreachEvent(
            event_id=stable_outreach_event_id(self._event_key_factory()),
            outreach_id=draft.outreach_id,
            revision=draft.revision,
            event_type=event_type,
            from_status=draft.status,
            to_status=target,
            created_at=self._clock(),
        )
        return self._repository.apply_outreach_event(event)

    def _telemetry(self, prompt_version: str, attempts: int, usage: Usage) -> OutreachTelemetry:
        if self._provider is None:
            raise RuntimeError("an LLM provider is required for generation telemetry")
        return OutreachTelemetry(
            provider=self._provider.name,
            prompt_version=prompt_version,
            attempts=attempts,
            repairs=attempts - 1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
