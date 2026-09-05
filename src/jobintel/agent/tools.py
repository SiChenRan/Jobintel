"""In-process toolbox backed by the canonical JobIntel tool contracts."""

from __future__ import annotations

import time
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from jobintel.errors import (
    EntityNotFoundError,
    EvidenceSearchError,
    IdempotencyConflictError,
    PersistenceValidationError,
)
from jobintel.models import JobAnalysisDraft, JobPosting, SearchCandidateEvidenceOutput
from jobintel.ports import JobRepository
from jobintel.provenance import (
    EntityKind,
    EntityRef,
    EvidenceSearchScope,
    ProvenanceLedger,
)
from jobintel.providers.base import ToolCall, ToolResultBlock, ToolSpec
from jobintel.services.analysis import (
    AnalysisFinalizationError,
    AnalysisService,
    AnalysisVersions,
)
from jobintel.services.evidence_search import EvidenceSearchService
from jobintel.services.jd_parser import JDParserError, JDParserService
from jobintel.tool_contracts import (
    STORED_REQUIREMENTS_PARSER_VERSION,
    TOOL_CONTRACT_BY_NAME,
    TOOL_CONTRACTS,
    CandidateProfileSummary,
    GetCandidateProfileRequest,
    GetCompanyRequest,
    GetJobRequest,
    ParsedJobRequirements,
    ParseJobRequirementsRequest,
    SaveAnalysisResult,
    SearchCandidateEvidenceRequest,
    ToolContract,
    ToolErrorCode,
    ToolErrorEnvelope,
)


class ToolExecutionError(Exception):
    """Transport-neutral expected tool failure with a stable JSON envelope."""

    def __init__(self, envelope: ToolErrorEnvelope) -> None:
        """Retain the typed envelope for every transport adapter."""
        self.envelope = envelope
        super().__init__(envelope.message)


class JobIntelToolbox:
    """Validate, execute, and provenance-track the six JobIntel tools."""

    def __init__(
        self,
        repository: JobRepository,
        ledger: ProvenanceLedger,
        *,
        evidence_search: EvidenceSearchService | None = None,
        analysis_service: AnalysisService | None = None,
        staged_job: JobPosting | None = None,
        jd_parser: JDParserService | None = None,
    ) -> None:
        """Bind one run-local ledger to persistence and deterministic services."""
        self.repository = repository
        self.ledger = ledger
        self._evidence_search = evidence_search or EvidenceSearchService()
        self._staged_job = staged_job
        self._jd_parser = jd_parser
        self._uses_default_analysis_service = analysis_service is None
        self._analysis_service = analysis_service or AnalysisService(
            repository, ledger, staged_job=staged_job
        )

    @staticmethod
    def specs() -> list[ToolSpec]:
        """Return provider-neutral specs projected from canonical contracts."""
        return [contract.provider_spec() for contract in TOOL_CONTRACTS]

    async def dispatch(self, call: ToolCall, *, iteration: int) -> ToolResultBlock:
        """Execute one provider-neutral call and serialize its typed result."""
        try:
            result = await self.execute(
                call.name,
                call.arguments,
                tool_call_id=call.id,
                iteration=iteration,
            )
        except ToolExecutionError as exc:
            return ToolResultBlock(
                tool_call_id=call.id,
                content=exc.envelope.model_dump_json(),
                is_error=True,
            )
        return ToolResultBlock(
            tool_call_id=call.id,
            content=result.model_dump_json(),
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
        iteration: int,
    ) -> BaseModel:
        """Validate and execute one call while recording content-free provenance."""
        if self.ledger.has_tool_call(tool_call_id):
            raise ToolExecutionError(
                ToolErrorEnvelope(
                    code=ToolErrorCode.DUPLICATE_TOOL_CALL,
                    message="tool_call_id was already executed in this run",
                    retryable=False,
                    field_path="tool_call_id",
                )
            )

        started = time.perf_counter_ns()
        contract = TOOL_CONTRACT_BY_NAME.get(name)
        if contract is None:
            error = ToolErrorEnvelope(
                code=ToolErrorCode.UNKNOWN_TOOL,
                message="unknown JobIntel tool",
                retryable=False,
                field_path="name",
                details={"tool_name": name},
            )
            self._record_failure(tool_call_id, name, arguments, error, iteration, started)
            raise ToolExecutionError(error)

        try:
            request = contract.request_model.model_validate(arguments)
        except ValidationError as exc:
            first_error = exc.errors(include_input=False)[0]
            field_path = ".".join(str(part) for part in first_error["loc"]) or None
            error = ToolErrorEnvelope(
                code=ToolErrorCode.INVALID_ARGUMENTS,
                message="tool arguments failed schema validation",
                retryable=True,
                field_path=field_path,
                details={
                    "error_count": exc.error_count(),
                    "error_type": first_error["type"],
                },
            )
            self._record_failure(tool_call_id, name, arguments, error, iteration, started)
            raise ToolExecutionError(error) from exc

        try:
            result = await self._invoke(contract, request)
        except _RawParserUnavailableError as exc:
            error = self._error(ToolErrorCode.PARSER_NOT_AVAILABLE, str(exc), retryable=False)
        except JDParserError as exc:
            error = ToolErrorEnvelope(
                code=ToolErrorCode.PARSER_REPAIR_LIMIT,
                message=str(exc),
                retryable=False,
                details={
                    "attempts": exc.telemetry.attempts,
                    "repairs": exc.telemetry.repairs,
                },
            )
        except EntityNotFoundError as exc:
            error = self._error(ToolErrorCode.NOT_FOUND, str(exc), retryable=False)
        except EvidenceSearchError as exc:
            error = self._error(ToolErrorCode.INVALID_SCOPE, str(exc), retryable=True)
        except AnalysisFinalizationError as exc:
            violations = [violation.model_dump(mode="json") for violation in exc.violations]
            error = ToolErrorEnvelope(
                code=ToolErrorCode.GUARDRAIL_REJECTED,
                message=str(exc),
                retryable=True,
                field_path=exc.violations[0].field_path if exc.violations else None,
                details={"violations": violations},
            )
        except IdempotencyConflictError as exc:
            error = self._error(ToolErrorCode.IDEMPOTENCY_CONFLICT, str(exc), retryable=False)
        except PersistenceValidationError as exc:
            error = self._error(ToolErrorCode.PERSISTENCE_REJECTED, str(exc), retryable=True)
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            error = self._error(
                ToolErrorCode.INTERNAL_ERROR,
                "unexpected internal tool failure",
                retryable=False,
            )
            self._record_failure(
                tool_call_id,
                name,
                request,
                error,
                iteration,
                started,
                search_scope=self._search_scope(request),
            )
            raise ToolExecutionError(error) from exc
        else:
            self._record_success(contract, request, result, tool_call_id, iteration, started)
            return result

        self._record_failure(
            tool_call_id,
            name,
            request,
            error,
            iteration,
            started,
            search_scope=self._search_scope(request),
        )
        raise ToolExecutionError(error)

    async def _invoke(self, contract: ToolContract, request: BaseModel) -> BaseModel:
        """Invoke the typed handler selected by its validated request model."""
        if isinstance(request, GetJobRequest):
            return self._get_job(request.job_id, request.job_version)
        if isinstance(request, ParseJobRequirementsRequest):
            return await self._parse_requirements(request)
        if isinstance(request, GetCandidateProfileRequest):
            profile = self.repository.get_candidate_profile(
                request.candidate_id, request.profile_version
            )
            return CandidateProfileSummary.from_profile(profile)
        if isinstance(request, SearchCandidateEvidenceRequest):
            return self._search_evidence(request)
        if isinstance(request, GetCompanyRequest):
            return self.repository.get_company(request.company_id)
        if isinstance(request, JobAnalysisDraft):
            return SaveAnalysisResult(analysis=self._analysis_service.finalize_and_save(request))
        raise RuntimeError(  # pragma: no cover - canonical contract invariant
            f"no handler for canonical contract {contract.name}"
        )

    async def _parse_requirements(
        self, request: ParseJobRequirementsRequest
    ) -> ParsedJobRequirements:
        """Expose stored requirements or stage provider-parsed raw JD input."""
        if request.jd_text is not None:
            if self._jd_parser is None:
                raise _RawParserUnavailableError
            parsed = await self._jd_parser.parse(request.jd_text)
            self._staged_job = parsed.job
            if self._uses_default_analysis_service:
                self._analysis_service = AnalysisService(
                    self.repository,
                    self.ledger,
                    versions=AnalysisVersions(parser=parsed.telemetry.prompt_version),
                    staged_job=parsed.job,
                )
            return ParsedJobRequirements(
                job_id=parsed.job.job_id,
                job_version=parsed.job.job_version,
                requirements=parsed.job.requirements,
                source_sha256=parsed.job.source_sha256,
                parser_version=parsed.telemetry.prompt_version,
            )
        job = self._get_job(cast(str, request.job_id), request.job_version)
        return ParsedJobRequirements(
            job_id=job.job_id,
            job_version=job.job_version,
            requirements=job.requirements,
            source_sha256=job.source_sha256,
            parser_version=STORED_REQUIREMENTS_PARSER_VERSION,
        )

    def _search_evidence(
        self, request: SearchCandidateEvidenceRequest
    ) -> SearchCandidateEvidenceOutput:
        """Resolve exact immutable scope before running deterministic retrieval."""
        job = self._get_job(request.job_id, request.job_version)
        requirement = next(
            (item for item in job.requirements if item.requirement_id == request.requirement_id),
            None,
        )
        if requirement is None:
            raise EvidenceSearchError(
                "requirement does not belong to the requested job version: "
                f"{request.requirement_id}"
            )
        profile = self.repository.get_candidate_profile(
            request.candidate_id, request.profile_version
        )
        return self._evidence_search.search(
            job=job,
            requirement=requirement,
            profile=profile,
            query=request.query,
            top_k=request.top_k,
        )

    def _get_job(self, job_id: str, job_version: int | None) -> JobPosting:
        """Resolve an exact staged raw job before falling back to persistence."""
        staged = self._staged_job
        if (
            staged is not None
            and staged.job_id == job_id
            and (job_version is None or staged.job_version == job_version)
        ):
            return staged
        return self.repository.get_job(job_id, job_version)

    def _record_success(
        self,
        contract: ToolContract,
        request: BaseModel,
        result: BaseModel,
        tool_call_id: str,
        iteration: int,
        started: int,
    ) -> None:
        """Record one successful result and any evidence receipts."""
        duration_ms = self._duration_ms(started)
        if isinstance(result, SearchCandidateEvidenceOutput):
            self.ledger.record_evidence_search(
                tool_call_id=tool_call_id,
                tool_input=request,
                output=result,
                iteration=iteration,
                duration_ms=duration_ms,
            )
            return
        self.ledger.record_observation(
            tool_call_id=tool_call_id,
            tool_name=contract.name,
            tool_input=request,
            tool_output=result,
            success=True,
            iteration=iteration,
            duration_ms=duration_ms,
            returned_entity_refs=self._entity_refs(result),
        )

    def _record_failure(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        error: ToolErrorEnvelope,
        iteration: int,
        started: int,
        *,
        search_scope: EvidenceSearchScope | None = None,
    ) -> None:
        """Record one structured failure without retaining raw input or output."""
        self.ledger.record_observation(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=error,
            success=False,
            error_code=error.code.value,
            iteration=iteration,
            duration_ms=self._duration_ms(started),
            evidence_search_scope=search_scope,
        )

    @staticmethod
    def _entity_refs(result: BaseModel) -> tuple[EntityRef, ...]:
        """Project successful read/compute responses to content-free references."""
        if isinstance(result, JobPosting):
            return (
                EntityRef(
                    kind=EntityKind.JOB,
                    entity_id=result.job_id,
                    version=result.job_version,
                ),
            )
        if isinstance(result, ParsedJobRequirements) and result.job_id is not None:
            return (
                EntityRef(
                    kind=EntityKind.JOB,
                    entity_id=result.job_id,
                    version=result.job_version,
                ),
            )
        if isinstance(result, CandidateProfileSummary):
            return (
                EntityRef(
                    kind=EntityKind.CANDIDATE_PROFILE,
                    entity_id=result.candidate_id,
                    version=result.profile_version,
                ),
            )
        if isinstance(result, SaveAnalysisResult):
            return ()
        company_id = getattr(result, "company_id", None)
        if isinstance(company_id, str):
            return (EntityRef(kind=EntityKind.COMPANY, entity_id=company_id),)
        return ()

    @staticmethod
    def _search_scope(request: BaseModel) -> EvidenceSearchScope | None:
        if not isinstance(request, SearchCandidateEvidenceRequest):
            return None
        return EvidenceSearchScope(
            job_id=request.job_id,
            job_version=request.job_version,
            requirement_id=request.requirement_id,
            candidate_id=request.candidate_id,
            profile_version=request.profile_version,
        )

    @staticmethod
    def _duration_ms(started: int) -> int:
        return max(0, (time.perf_counter_ns() - started) // 1_000_000)

    @staticmethod
    def _error(code: ToolErrorCode, message: str, *, retryable: bool) -> ToolErrorEnvelope:
        return ToolErrorEnvelope(code=code, message=message, retryable=retryable)


class _RawParserUnavailableError(EvidenceSearchError):
    """Internal marker mapped to the stable M4 parser availability error."""

    def __init__(self) -> None:
        super().__init__("raw JD parsing requires a configured JD parser")
