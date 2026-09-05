"""Provider-neutral explicit state machine for JobIntel analysis runs."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import ConfigDict, Field

from jobintel.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_analysis_goal
from jobintel.agent.tools import JobIntelToolbox
from jobintel.config import JobIntelSettings
from jobintel.errors import JobIntelError
from jobintel.models import FrozenDomainModel, JobAnalysis, NonEmptyStr
from jobintel.ports import JobRepository
from jobintel.provenance import ProvenanceLedger
from jobintel.providers.base import LLMProvider, Message, ToolCall, ToolResultBlock, Usage
from jobintel.services.analysis import AnalysisService, AnalysisVersions
from jobintel.services.intake import (
    AnalysisIntakeService,
    AnalysisRequest,
    ResolvedAnalysisIntake,
)
from jobintel.services.jd_parser import JDParserError, JDParserService, ParserTelemetry
from jobintel.tool_contracts import (
    SaveAnalysisResult,
    ToolErrorCode,
    ToolErrorEnvelope,
)

_TERMINAL_TOOL = "save_application_analysis"


class AgentState(StrEnum):
    """Observable states in the JobIntel run state machine."""

    INITIALIZE = "initialize"
    MODEL_TURN = "model_turn"
    CLASSIFY_TURN = "classify_turn"
    EXECUTE_TOOLS = "execute_tools"
    VALIDATE_DRAFT = "validate_draft"
    GUARDRAIL = "guardrail"
    SCORE = "score"
    PERSIST = "persist"
    APPEND_RESULTS = "append_results"
    NO_TOOL_NUDGE = "no_tool_nudge"
    REPAIR = "repair"
    COMPLETE = "complete"
    FAILED = "failed"


class AgentFailureCode(StrEnum):
    """Stable terminal failure reasons for offline and CLI callers."""

    INTAKE_FAILED = "INTAKE_FAILED"
    PARSER_REPAIR_LIMIT = "PARSER_REPAIR_LIMIT"
    REPAIR_LIMIT = "REPAIR_LIMIT"
    TERMINAL_FAILED = "TERMINAL_FAILED"
    TOOL_CALL_LIMIT = "TOOL_CALL_LIMIT"
    ITERATION_LIMIT = "ITERATION_LIMIT"


class AgentTraceEvent(FrozenDomainModel):
    """Content-free snapshot emitted on every state transition."""

    state: AgentState
    iteration: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    code: NonEmptyStr | None = None


class AgentRunTelemetry(FrozenDomainModel):
    """Structured, privacy-safe telemetry for one complete or failed run."""

    provider: NonEmptyStr
    prompt_version: NonEmptyStr = PROMPT_VERSION
    iterations: int = Field(ge=0)
    repairs: int = Field(ge=0)
    tool_calls: tuple[NonEmptyStr, ...]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    parser: ParserTelemetry | None = None
    trace: tuple[AgentTraceEvent, ...]


class JobIntelAgentResult(FrozenDomainModel):
    """Final persisted analysis and deterministic run telemetry."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    analysis: JobAnalysis
    telemetry: AgentRunTelemetry


class JobIntelAgentError(RuntimeError):
    """Structured run failure retaining content-free telemetry when available."""

    def __init__(
        self,
        code: AgentFailureCode,
        message: str,
        telemetry: AgentRunTelemetry | None = None,
    ) -> None:
        """Retain a stable code and optional run telemetry."""
        self.code = code
        self.telemetry = telemetry
        super().__init__(message)


class JobIntelAgent:
    """Run JobIntel tools until one exclusive terminal save succeeds."""

    def __init__(
        self,
        provider: LLMProvider,
        repository: JobRepository,
        settings: JobIntelSettings,
        *,
        parser_provider: LLMProvider | None = None,
        intake_service: AnalysisIntakeService | None = None,
    ) -> None:
        """Wire provider, repository, budgets, and optional parser dependencies."""
        self._provider = provider
        self._repository = repository
        self._settings = settings
        parser = JDParserService(
            parser_provider or provider,
            max_repairs=settings.parser_max_repairs,
        )
        self._intake = intake_service or AnalysisIntakeService(repository, parser)

    async def analyze(
        self, request: AnalysisRequest, *, dry_run: bool = False
    ) -> JobIntelAgentResult:
        """Resolve input and execute the bounded explicit state machine."""
        try:
            intake = await self._intake.resolve(request)
        except JDParserError as exc:
            telemetry = self._intake_failure_telemetry(exc.telemetry)
            raise JobIntelAgentError(
                AgentFailureCode.PARSER_REPAIR_LIMIT,
                str(exc),
                telemetry,
            ) from exc
        except (JobIntelError, RuntimeError) as exc:
            raise JobIntelAgentError(
                AgentFailureCode.INTAKE_FAILED,
                "analysis intake could not resolve the requested scope",
            ) from exc
        return await self._run(request, intake, dry_run=dry_run)

    async def _run(
        self,
        request: AnalysisRequest,
        intake: ResolvedAnalysisIntake,
        *,
        dry_run: bool,
    ) -> JobIntelAgentResult:
        ledger = ProvenanceLedger(request.run_id)
        versions = AnalysisVersions(
            prompt=PROMPT_VERSION,
            parser=intake.parser_version,
        )
        analysis_service = AnalysisService(
            self._repository,
            ledger,
            versions=versions,
            staged_job=intake.job if intake.is_raw_job else None,
            persist=not dry_run,
        )
        toolbox = JobIntelToolbox(
            self._repository,
            ledger,
            analysis_service=analysis_service,
            staged_job=intake.job if intake.is_raw_job else None,
        )
        messages: list[Message] = [Message.user_text(build_analysis_goal(intake))]
        usage = self._parser_usage(intake.parser_telemetry)
        called: list[str] = []
        trace: list[AgentTraceEvent] = []
        repairs = 0
        iteration = 0
        self._trace(trace, AgentState.INITIALIZE, iteration, called, repairs)

        for iteration in range(1, self._settings.agent_max_iterations + 1):
            self._trace(trace, AgentState.MODEL_TURN, iteration, called, repairs)
            turn = await self._provider.run_turn(SYSTEM_PROMPT, messages, toolbox.specs())
            usage = usage + turn.usage
            messages.append(turn.assistant_message())
            self._trace(trace, AgentState.CLASSIFY_TURN, iteration, called, repairs)

            if not turn.wants_tools:
                self._trace(trace, AgentState.NO_TOOL_NUDGE, iteration, called, repairs)
                messages.append(
                    Message.user_text(
                        "Continue with the tools. Completion requires one exclusive successful "
                        "save_application_analysis call."
                    )
                )
                continue

            called.extend(call.name for call in turn.tool_calls)
            if len(called) > self._settings.agent_max_tool_calls:
                return self._fail(
                    AgentFailureCode.TOOL_CALL_LIMIT,
                    "agent exceeded its tool call budget",
                    intake,
                    usage,
                    iteration,
                    called,
                    repairs,
                    trace,
                )
            terminal_calls = [call for call in turn.tool_calls if call.name == _TERMINAL_TOOL]

            if terminal_calls and len(turn.tool_calls) != 1:
                repairs += 1
                self._trace(
                    trace,
                    AgentState.REPAIR,
                    iteration,
                    called,
                    repairs,
                    ToolErrorCode.INVALID_TERMINAL_TURN.value,
                )
                if repairs > self._settings.agent_max_repairs:
                    return self._fail(
                        AgentFailureCode.REPAIR_LIMIT,
                        "agent exceeded its terminal repair budget",
                        intake,
                        usage,
                        iteration,
                        called,
                        repairs,
                        trace,
                    )
                messages.append(
                    Message(
                        role="user",
                        blocks=self._mixed_terminal_errors(turn.tool_calls),
                    )
                )
                continue

            self._trace(trace, AgentState.EXECUTE_TOOLS, iteration, called, repairs)
            if terminal_calls:
                self._trace(trace, AgentState.VALIDATE_DRAFT, iteration, called, repairs)
            results = [
                await toolbox.dispatch(call, iteration=iteration) for call in turn.tool_calls
            ]
            if terminal_calls:
                result = results[0]
                if not result.is_error:
                    saved = SaveAnalysisResult.model_validate_json(result.content)
                    self._trace(trace, AgentState.GUARDRAIL, iteration, called, repairs)
                    self._trace(trace, AgentState.SCORE, iteration, called, repairs)
                    self._trace(trace, AgentState.PERSIST, iteration, called, repairs)
                    self._trace(trace, AgentState.COMPLETE, iteration, called, repairs)
                    return JobIntelAgentResult(
                        analysis=saved.analysis,
                        telemetry=self._telemetry(intake, usage, iteration, called, repairs, trace),
                    )
                terminal_error = ToolErrorEnvelope.model_validate_json(result.content)
                if not terminal_error.retryable:
                    return self._fail(
                        AgentFailureCode.TERMINAL_FAILED,
                        "terminal tool returned a non-retryable error",
                        intake,
                        usage,
                        iteration,
                        called,
                        repairs,
                        trace,
                    )
                repairs += 1
                self._trace(trace, AgentState.REPAIR, iteration, called, repairs)
                if repairs > self._settings.agent_max_repairs:
                    return self._fail(
                        AgentFailureCode.REPAIR_LIMIT,
                        "agent exceeded its terminal repair budget",
                        intake,
                        usage,
                        iteration,
                        called,
                        repairs,
                        trace,
                    )

            self._trace(trace, AgentState.APPEND_RESULTS, iteration, called, repairs)
            messages.append(Message(role="user", blocks=results))

        return self._fail(
            AgentFailureCode.ITERATION_LIMIT,
            "agent did not complete within its iteration budget",
            intake,
            usage,
            iteration,
            called,
            repairs,
            trace,
        )

    @staticmethod
    def _mixed_terminal_errors(calls: Sequence[ToolCall]) -> list[ToolResultBlock]:
        envelope = ToolErrorEnvelope(
            code=ToolErrorCode.INVALID_TERMINAL_TURN,
            message="save_application_analysis must be the only tool call in its turn",
            retryable=True,
        )
        return [
            ToolResultBlock(
                tool_call_id=call.id,
                content=envelope.model_dump_json(),
                is_error=True,
            )
            for call in calls
        ]

    def _fail(
        self,
        code: AgentFailureCode,
        message: str,
        intake: ResolvedAnalysisIntake,
        usage: Usage,
        iteration: int,
        called: list[str],
        repairs: int,
        trace: list[AgentTraceEvent],
    ) -> JobIntelAgentResult:
        """Trace and raise one structured bounded-run failure."""
        self._trace(trace, AgentState.FAILED, iteration, called, repairs, code.value)
        raise JobIntelAgentError(
            code,
            message,
            self._telemetry(intake, usage, iteration, called, repairs, trace),
        )

    def _intake_failure_telemetry(self, parser: ParserTelemetry) -> AgentRunTelemetry:
        """Build structured telemetry when parsing fails before agent iteration one."""
        return AgentRunTelemetry(
            provider=self._provider.name,
            iterations=0,
            repairs=0,
            tool_calls=(),
            input_tokens=parser.input_tokens,
            output_tokens=parser.output_tokens,
            parser=parser,
            trace=(
                AgentTraceEvent(
                    state=AgentState.FAILED,
                    iteration=0,
                    tool_call_count=0,
                    repair_count=0,
                    code=AgentFailureCode.PARSER_REPAIR_LIMIT.value,
                ),
            ),
        )

    def _telemetry(
        self,
        intake: ResolvedAnalysisIntake,
        usage: Usage,
        iterations: int,
        called: list[str],
        repairs: int,
        trace: list[AgentTraceEvent],
    ) -> AgentRunTelemetry:
        return AgentRunTelemetry(
            provider=self._provider.name,
            iterations=iterations,
            repairs=repairs,
            tool_calls=tuple(called),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            parser=intake.parser_telemetry,
            trace=tuple(trace),
        )

    @staticmethod
    def _parser_usage(parser: ParserTelemetry | None) -> Usage:
        if parser is None:
            return Usage()
        return Usage(
            input_tokens=parser.input_tokens,
            output_tokens=parser.output_tokens,
        )

    @staticmethod
    def _trace(
        trace: list[AgentTraceEvent],
        state: AgentState,
        iteration: int,
        called: list[str],
        repairs: int,
        code: str | None = None,
    ) -> None:
        trace.append(
            AgentTraceEvent(
                state=state,
                iteration=iteration,
                tool_call_count=len(called),
                repair_count=repairs,
                code=code,
            )
        )
