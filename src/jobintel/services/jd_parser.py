"""Provider-neutral structured extraction for raw job descriptions."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import Field, ValidationError, model_validator

from jobintel.models import (
    FrozenDomainModel,
    JobPosting,
    JobRequirement,
    NonEmptyStr,
    RequirementCategory,
    RequirementImportance,
    canonicalize_requirement_text,
    stable_requirement_id,
)
from jobintel.providers.base import (
    LLMProvider,
    Message,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    Usage,
)

PARSER_PROMPT_VERSION = "jobintel-jd-parser-v3-hard-requirements-zh-cn"
PARSER_SUBMIT_TOOL = "submit_parsed_job"

PARSER_SYSTEM_PROMPT = (
    "You extract a job description into a strict structured object. Preserve each explicit "
    "requirement as a concise standalone statement, in source order. Classify category and "
    "importance from the text; do not invent requirements. Split concrete technical skills "
    "such as Python, LangChain, RAG, databases, frameworks, tools, and protocols into distinct "
    "skill requirements and set normalized_skill. Use normalized_skill only for a named, "
    "verifiable professional skill, never for soft skills, generic responsibilities, business "
    "outcomes, collaboration style, or product vision. Classify statements such as helping turn "
    "a prototype into a user-facing product or translating user needs into product features as "
    "other unless they also state a separate concrete technical threshold. Generic duties must "
    "not be converted into skill requirements. A named technical skill listed under job "
    "requirements is must unless "
    "the source explicitly marks it preferred, optional, or bonus. "
    "Preserve must, preferred, optional, and bonus wording in each requirement text so the "
    "scoring policy can audit that modality. "
    "Infer a short title and company name when present; otherwise use '未命名职位' and "
    "'未知公司'. Requirement text must use concise Simplified Chinese; "
    "preserve technical names such as Python, Agent, RAG, APIs, and framework names. Finish "
    "by calling submit_parsed_job exactly once. Do not return prose or any other tool call."
)


class ExtractedRequirement(FrozenDomainModel):
    """Model-extracted requirement before program-controlled identity assignment."""

    text: NonEmptyStr
    category: RequirementCategory
    importance: RequirementImportance
    normalized_skill: NonEmptyStr | None = None

    @model_validator(mode="after")
    def concrete_skill_has_normalized_name(self) -> ExtractedRequirement:
        """Force the parser to name every item it classifies as a hard skill."""
        if self.category is RequirementCategory.SKILL and self.normalized_skill is None:
            raise ValueError("skill requirement requires normalized_skill")
        return self


class ParsedJobSubmission(FrozenDomainModel):
    """Private parser terminal payload authored by the extraction model."""

    company_name: NonEmptyStr
    title: NonEmptyStr
    location: NonEmptyStr | None = None
    employment_type: NonEmptyStr | None = None
    requirements: tuple[ExtractedRequirement, ...] = Field(min_length=1)


class ParserTelemetry(FrozenDomainModel):
    """Content-free telemetry for one bounded parser run."""

    prompt_version: NonEmptyStr = PARSER_PROMPT_VERSION
    attempts: int = Field(ge=1)
    repairs: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ParsedJobResult(FrozenDomainModel):
    """Program-identified transient job plus parser telemetry."""

    job: JobPosting
    telemetry: ParserTelemetry


class JDParserError(RuntimeError):
    """Raised when structured extraction exhausts its bounded repair budget."""

    def __init__(self, telemetry: ParserTelemetry) -> None:
        """Retain content-free telemetry for caller diagnostics."""
        self.telemetry = telemetry
        super().__init__(
            f"JD parser failed after {telemetry.attempts} attempts and {telemetry.repairs} repairs"
        )


def normalize_raw_jd(jd_text: str) -> str:
    """Normalize source edges without changing meaningful internal text."""
    normalized = "\n".join(line.rstrip() for line in jd_text.strip().splitlines())
    if not normalized:
        raise ValueError("jd_text must not be empty")
    return normalized


def raw_job_id(jd_text: str) -> str:
    """Derive a stable raw-job identity from normalized source text."""
    normalized = normalize_raw_jd(jd_text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"job_raw_{digest[:20]}"


def raw_source_sha256(jd_text: str) -> str:
    """Hash the normalized accepted JD payload for immutable source identity."""
    normalized = normalize_raw_jd(jd_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class JDParserService:
    """Drive an LLMProvider through one bounded structured extraction loop."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_repairs: int = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure provider, repair budget, and deterministic test clock."""
        if max_repairs < 0:
            raise ValueError("max_repairs must be non-negative")
        self._provider = provider
        self._max_repairs = max_repairs
        self._clock = clock or (lambda: datetime.now(UTC))

    async def parse(self, jd_text: str, *, source_url: str | None = None) -> ParsedJobResult:
        """Extract a raw JD, repairing malformed model output within budget."""
        normalized_jd = normalize_raw_jd(jd_text)
        job_id = raw_job_id(normalized_jd)
        source_sha256 = raw_source_sha256(normalized_jd)
        messages: list[Message] = [
            Message.user_text(
                "请将以下职位描述提取为指定结构, 要求条目使用简体中文:\n\n" + normalized_jd
            )
        ]
        usage = Usage()
        spec = self._tool_spec()

        for attempt in range(1, self._max_repairs + 2):
            turn = await self._provider.run_turn(PARSER_SYSTEM_PROMPT, messages, [spec])
            usage = usage + turn.usage
            messages.append(turn.assistant_message())
            submission, error_results = self._submission(turn.tool_calls)
            if submission is not None:
                job = self._materialize(
                    submission,
                    job_id=job_id,
                    jd_text=normalized_jd,
                    source_url=source_url,
                    source_sha256=source_sha256,
                )
                return ParsedJobResult(
                    job=job,
                    telemetry=self._telemetry(attempt, usage),
                )
            if attempt <= self._max_repairs:
                if error_results:
                    messages.append(Message(role="user", blocks=error_results))
                else:
                    messages.append(
                        Message.user_text(
                            "Parser output was missing. Call submit_parsed_job exactly once "
                            "with arguments matching its schema."
                        )
                    )

        raise JDParserError(self._telemetry(self._max_repairs + 1, usage))

    @staticmethod
    def _tool_spec() -> ToolSpec:
        schema = ParsedJobSubmission.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(
            name=PARSER_SUBMIT_TOOL,
            description="Submit the structured fields extracted from one raw job description.",
            input_schema=schema,
        )

    @staticmethod
    def _submission(
        calls: Sequence[ToolCall],
    ) -> tuple[ParsedJobSubmission | None, list[ToolResultBlock]]:
        """Accept exactly one valid parser terminal call or build safe repair results."""
        if len(calls) == 1 and calls[0].name == PARSER_SUBMIT_TOOL:
            call = calls[0]
            try:
                return ParsedJobSubmission.model_validate(call.arguments), []
            except ValidationError as exc:
                first = exc.errors(include_input=False)[0]
                field_path = ".".join(str(item) for item in first["loc"])
                content = (
                    '{"code":"INVALID_PARSED_JOB","message":"parser output failed schema '
                    f'validation","retryable":true,"field_path":"{field_path}"}}'
                )
                return None, [ToolResultBlock(tool_call_id=call.id, content=content, is_error=True)]

        results = [
            ToolResultBlock(
                tool_call_id=call.id,
                content=(
                    '{"code":"INVALID_PARSER_TURN","message":"call '
                    'submit_parsed_job exactly once","retryable":true}'
                ),
                is_error=True,
            )
            for call in calls
        ]
        return None, results

    def _materialize(
        self,
        submission: ParsedJobSubmission,
        *,
        job_id: str,
        jd_text: str,
        source_url: str | None,
        source_sha256: str,
    ) -> JobPosting:
        """Assign stable requirement identities and construct a transient revision."""
        occurrences: defaultdict[tuple[str, RequirementCategory], int] = defaultdict(int)
        requirements = []
        for source_order, extracted in enumerate(submission.requirements):
            key = (canonicalize_requirement_text(extracted.text), extracted.category)
            occurrences[key] += 1
            requirements.append(
                JobRequirement(
                    requirement_id=stable_requirement_id(
                        job_id=job_id,
                        job_version=1,
                        text=extracted.text,
                        category=extracted.category,
                        duplicate_ordinal=occurrences[key],
                    ),
                    **extracted.model_dump(),
                    source_order=source_order,
                )
            )
        return JobPosting(
            job_id=job_id,
            job_version=1,
            company_name=submission.company_name,
            title=submission.title,
            location=submission.location,
            employment_type=submission.employment_type,
            description=normalize_raw_jd(jd_text),
            requirements=tuple(requirements),
            source_url=source_url,
            source_sha256=source_sha256,
            created_at=self._clock(),
        )

    @staticmethod
    def _telemetry(attempts: int, usage: Usage) -> ParserTelemetry:
        return ParserTelemetry(
            attempts=attempts,
            repairs=attempts - 1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
