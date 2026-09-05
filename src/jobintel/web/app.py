"""FastAPI composition root for the local JobIntel web workspace."""

from __future__ import annotations

import re
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from jobintel.config import JobIntelProviderName, JobIntelSettings, load_jobintel_settings
from jobintel.discovery.connectors.cdp import cdp_reachable
from jobintel.discovery.connectors.registry import build_connectors
from jobintel.discovery.models import EmploymentType, JobSearchPreference, JobSource
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import JobIntelError
from jobintel.models import FrozenDomainModel, JobAnalysis, NonEmptyStr
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MigrationRunner
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database
from jobintel.providers.factory import build_jobintel_provider
from jobintel.services.discovery_analysis import analyze_discovery_hits
from jobintel.services.radar import JobRadarService
from jobintel.services.resume_parser import (
    MAX_RESUME_BYTES,
    CandidateProfilePreview,
    ResumeParserService,
    materialize_profile,
)

_STATIC_DIR = Path(__file__).with_name("static")
_PREVIEW_ID = re.compile(r"web-[0-9a-f]{32}\.json")
_LIVE_SOURCE_LOCK = threading.Lock()


class DiscoveryRequest(FrozenDomainModel):
    """Browser-submitted discovery form after strict API validation."""

    candidate_id: NonEmptyStr
    profile_version: int | None = Field(default=None, ge=1)
    query: NonEmptyStr = Field(max_length=100)
    city: str = Field(default="", max_length=50)
    salary_min_k: int | None = Field(default=None, ge=0, le=1000)
    salary_max_k: int | None = Field(default=None, ge=0, le=1000)
    daily_salary_min_yuan: int | None = Field(default=None, ge=0, le=10000)
    daily_salary_max_yuan: int | None = Field(default=None, ge=0, le=10000)
    employment_types: tuple[EmploymentType, ...] = ()
    education_requirements: tuple[NonEmptyStr, ...] = ()
    experience_requirements: tuple[NonEmptyStr, ...] = ()
    exclusions: tuple[NonEmptyStr, ...] = ()
    exclude_outsourcing: bool = False
    exclude_training: bool = False
    exclude_agency: bool = False
    strict_salary: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    detail_top: int = Field(default=3, ge=0, le=10)


class AnalyzeDiscoveryRequest(FrozenDomainModel):
    """Options for analyzing a saved discovery batch."""

    top: int = Field(default=3, ge=1, le=10)
    provider: JobIntelProviderName | None = None
    dry_run: bool = False


class ConfirmProfileRequest(FrozenDomainModel):
    """Reference to a server-owned profile preview artifact."""

    preview_id: NonEmptyStr


class RadarRequest(FrozenDomainModel):
    """Options for a cooldown-protected radar refresh."""

    baseline_run_id: NonEmptyStr
    detail_top: int = Field(default=0, ge=0, le=10)
    force: bool = False


def _with_provider(
    settings: JobIntelSettings, provider: JobIntelProviderName | None
) -> JobIntelSettings:
    """Return request-local settings with an optional provider override."""
    return settings.model_copy(update={"llm_provider": provider}) if provider else settings


@contextmanager
def _repository(settings: JobIntelSettings) -> Iterator[SQLiteJobRepository]:
    """Open a migrated repository for one HTTP request and always close it."""
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        row = database.connection.execute(
            "SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM candidate_profiles)"
        ).fetchone()
        if int(row[0]) == 0 and int(row[1]) == 0:
            seed_database(database)
        yield SQLiteJobRepository(database)
    finally:
        database.close()


def _discovery_service(
    repository: SQLiteJobRepository, settings: JobIntelSettings
) -> JobDiscoveryService:
    """Build the exact rate-limited connector stack used by the CLI."""
    return JobDiscoveryService(
        repository,
        build_connectors(
            cdp_port=settings.discovery_cdp_port,
            search_min_delay_seconds=settings.discovery_search_min_delay_seconds,
            search_max_delay_seconds=settings.discovery_search_max_delay_seconds,
            detail_min_delay_seconds=settings.discovery_detail_min_delay_seconds,
            detail_max_delay_seconds=settings.discovery_detail_max_delay_seconds,
        ),
        max_workers=settings.discovery_max_workers,
    )


def _error(status_code: int, code: str, exc: Exception) -> HTTPException:
    """Build one stable API error without exposing stack traces."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": str(exc)})


def _preview_path(settings: JobIntelSettings, preview_id: str) -> Path:
    """Resolve only server-issued preview identifiers inside the private directory."""
    if _PREVIEW_ID.fullmatch(preview_id) is None:
        raise ValueError("invalid profile preview identifier")
    return settings.jobintel_db_path.parent / "profile-previews" / preview_id


def _exclusions(request: DiscoveryRequest) -> tuple[str, ...]:
    """Expand transparent UI presets into the same keyword filters as the CLI."""
    values = list(request.exclusions)
    if request.exclude_outsourcing:
        values.extend(("外包", "驻场"))
    if request.exclude_training:
        values.extend(("培训机构", "岗前培训", "付费培训", "实训"))
    if request.exclude_agency:
        values.extend(("猎头", "人力资源", "人才服务"))
    return tuple(dict.fromkeys(values))


def _analysis_payload(repository: SQLiteJobRepository, analysis: JobAnalysis) -> dict[str, object]:
    """Add human-readable job and requirement text to an analysis payload."""
    payload = analysis.model_dump(mode="json")
    payload["job"] = repository.get_job(analysis.job_id, analysis.job_version).model_dump(
        mode="json"
    )
    return payload


def create_app(settings: JobIntelSettings | None = None) -> FastAPI:
    """Create the local-only JobIntel web application."""
    configured = settings or load_jobintel_settings()
    application = FastAPI(
        title="JobIntel 工作台",
        version="1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    application.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve the single-page workspace shell."""
        return FileResponse(_STATIC_DIR / "index.html")

    @application.get("/api/health")
    def health() -> dict[str, object]:
        """Report local prerequisites without contacting BOSS."""
        return {
            "ok": True,
            "boss_browser_ready": cdp_reachable(configured.discovery_cdp_port),
            "provider": configured.llm_provider.value,
            "radar_interval_hours": configured.radar_min_interval_hours,
        }

    @application.get("/api/dashboard")
    def dashboard() -> dict[str, object]:
        """Return compact data for the landing dashboard."""
        with _repository(configured) as repository:
            profiles = repository.list_candidate_profiles()
            discoveries = repository.list_discovery_runs(limit=8)
            analyses = repository.list_analyses(limit=8)
            radar_checks = repository.list_radar_checks(limit=8)
            return {
                "counts": repository.dashboard_counts(),
                "profiles": [
                    {
                        "candidate_id": profile.candidate_id,
                        "profile_version": profile.profile_version,
                        "summary": profile.summary,
                        "evidence_count": len(profile.evidence),
                        "created_at": profile.created_at,
                    }
                    for profile in profiles
                ],
                "discoveries": [
                    {
                        "run_id": run.run_id,
                        "candidate_id": run.preference.candidate_id,
                        "query": run.preference.query,
                        "city": run.preference.city,
                        "hit_count": len(run.hits),
                        "created_at": run.created_at,
                    }
                    for run in discoveries
                ],
                "analyses": [
                    {
                        "analysis_id": item.analysis_id,
                        "candidate_id": item.candidate_id,
                        "job_id": item.job_id,
                        "job_title": repository.get_job(item.job_id, item.job_version).title,
                        "company_name": repository.get_job(
                            item.job_id, item.job_version
                        ).company_name,
                        "score": item.score,
                        "recommendation": item.recommendation,
                        "created_at": item.created_at,
                    }
                    for item in analyses
                ],
                "radar_checks": [
                    {
                        "run_id": check.run_id,
                        "baseline_run_id": check.baseline_run_id,
                        "event_count": len(check.events),
                        "created_at": check.created_at,
                    }
                    for check in radar_checks
                ],
            }

    @application.get("/api/profiles")
    def profiles() -> list[dict[str, object]]:
        """List latest candidate profiles."""
        with _repository(configured) as repository:
            return [item.model_dump(mode="json") for item in repository.list_candidate_profiles()]

    @application.get("/api/profiles/{candidate_id}")
    def profile(candidate_id: str, version: int | None = Query(default=None, ge=1)) -> object:
        """Return one complete candidate profile."""
        try:
            with _repository(configured) as repository:
                return repository.get_candidate_profile(candidate_id, version).model_dump(
                    mode="json"
                )
        except JobIntelError as exc:
            raise _error(404, "PROFILE_NOT_FOUND", exc) from exc

    @application.post("/api/profiles/preview")
    async def preview_profile(
        candidate_id: Annotated[str, Form()],
        resume: Annotated[UploadFile, File()],
        provider: Annotated[JobIntelProviderName | None, Form()] = None,
    ) -> dict[str, object]:
        """Parse an uploaded resume and retain a private, reviewable preview."""
        candidate_id = candidate_id.strip()
        if not candidate_id:
            raise _error(422, "INVALID_CANDIDATE", ValueError("candidate_id is required"))
        filename = Path(resume.filename or "resume.txt").name
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".pdf", ".txt", ".md", ".markdown"}:
            raise _error(422, "UNSUPPORTED_RESUME", ValueError("仅支持 PDF、TXT、Markdown 简历"))
        content = await resume.read(MAX_RESUME_BYTES + 1)
        if len(content) > MAX_RESUME_BYTES:
            raise _error(413, "RESUME_TOO_LARGE", ValueError("简历文件不能超过 10 MiB"))
        request_settings = _with_provider(configured, provider)
        try:
            llm = build_jobintel_provider(request_settings)
            with _repository(request_settings) as repository:
                version = repository.next_candidate_profile_version(candidate_id)
                with tempfile.TemporaryDirectory(prefix="jobintel-resume-") as directory:
                    resume_path = Path(directory) / f"resume{suffix}"
                    resume_path.write_bytes(content)
                    parsed = await ResumeParserService(
                        llm, max_repairs=request_settings.parser_max_repairs
                    ).preview(
                        candidate_id=candidate_id,
                        profile_version=version,
                        resume_file=resume_path,
                    )
                preview = parsed.preview.model_copy(update={"source_name": filename})
                preview_id = f"web-{uuid.uuid4().hex}.json"
                output_path = _preview_path(request_settings, preview_id)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(preview.model_dump_json(indent=2), encoding="utf-8")
                output_path.chmod(0o600)
            return {
                "preview_id": preview_id,
                "preview": preview.model_dump(mode="json"),
                "telemetry": parsed.telemetry.model_dump(mode="json"),
                "persisted": False,
            }
        except (JobIntelError, OSError, RuntimeError, ValueError) as exc:
            raise _error(400, "PROFILE_PREVIEW_FAILED", exc) from exc
        finally:
            await resume.close()

    @application.post("/api/profiles/confirm")
    def confirm_profile(request: ConfirmProfileRequest) -> object:
        """Atomically append a reviewed server-owned preview as a profile version."""
        try:
            path = _preview_path(configured, request.preview_id)
            preview = CandidateProfilePreview.model_validate_json(path.read_text(encoding="utf-8"))
            with _repository(configured) as repository:
                saved = repository.save_candidate_profile(materialize_profile(preview))
            return saved.model_dump(mode="json")
        except FileNotFoundError as exc:
            raise _error(404, "PROFILE_PREVIEW_NOT_FOUND", exc) from exc
        except (JobIntelError, OSError, ValueError) as exc:
            raise _error(400, "PROFILE_CONFIRM_FAILED", exc) from exc

    @application.post("/api/discoveries")
    def discover(request: DiscoveryRequest) -> dict[str, object]:
        """Run one bounded BOSS discovery using the existing protected connector."""
        try:
            preference = JobSearchPreference(
                candidate_id=request.candidate_id,
                profile_version=request.profile_version,
                query=request.query,
                city=request.city,
                salary_min_k=request.salary_min_k,
                salary_max_k=request.salary_max_k,
                daily_salary_min_yuan=request.daily_salary_min_yuan,
                daily_salary_max_yuan=request.daily_salary_max_yuan,
                include_undisclosed_salary=not request.strict_salary,
                employment_types=request.employment_types,
                education_requirements=request.education_requirements,
                experience_requirements=request.experience_requirements,
                exclusions=_exclusions(request),
                sources=(JobSource.BOSS,),
                limit=request.limit,
            )
            if not _LIVE_SOURCE_LOCK.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "SOURCE_BUSY", "message": "已有职位抓取正在运行"},
                )
            try:
                with _repository(configured) as repository:
                    run = _discovery_service(repository, configured).discover(
                        preference,
                        detail_limit=request.detail_top,
                        detail_cache_hours=configured.discovery_detail_cache_hours,
                    )
            finally:
                _LIVE_SOURCE_LOCK.release()
            return {"discovery": run.model_dump(mode="json")}
        except HTTPException:
            raise
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "DISCOVERY_FAILED", exc) from exc

    @application.get("/api/discoveries")
    def discoveries(
        candidate_id: str | None = None, limit: int = Query(default=20, ge=1, le=200)
    ) -> list[dict[str, object]]:
        """List saved discovery runs."""
        with _repository(configured) as repository:
            return [
                item.model_dump(mode="json")
                for item in repository.list_discovery_runs(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/discoveries/{run_id}")
    def discovery(run_id: str) -> object:
        """Return one complete saved discovery run."""
        try:
            with _repository(configured) as repository:
                return repository.get_discovery_run(run_id).model_dump(mode="json")
        except JobIntelError as exc:
            raise _error(404, "DISCOVERY_NOT_FOUND", exc) from exc

    @application.post("/api/discoveries/{run_id}/analyze")
    def analyze_discovery(run_id: str, request: AnalyzeDiscoveryRequest) -> object:
        """Analyze saved jobs without touching BOSS again."""
        request_settings = _with_provider(configured, request.provider)
        try:
            llm = build_jobintel_provider(request_settings)
            with _repository(request_settings) as repository:
                run = repository.get_discovery_run(run_id)
                analyses = analyze_discovery_hits(
                    run,
                    analyze_top=request.top,
                    repository=repository,
                    settings=request_settings,
                    dry_run=request.dry_run,
                    llm=llm,
                )
            return {"run_id": run_id, "analyses": analyses, "dry_run": request.dry_run}
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "ANALYZE_DISCOVERY_FAILED", exc) from exc

    @application.get("/api/analyses")
    def analyses(
        candidate_id: str | None = None, limit: int = Query(default=50, ge=1, le=500)
    ) -> list[dict[str, object]]:
        """List complete persisted analyses."""
        with _repository(configured) as repository:
            return [
                _analysis_payload(repository, item)
                for item in repository.list_analyses(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/analyses/{analysis_id}")
    def analysis(analysis_id: str) -> object:
        """Return one complete persisted analysis."""
        try:
            with _repository(configured) as repository:
                return _analysis_payload(repository, repository.get_analysis(analysis_id))
        except JobIntelError as exc:
            raise _error(404, "ANALYSIS_NOT_FOUND", exc) from exc

    @application.post("/api/radar/checks")
    def check_radar(request: RadarRequest) -> object:
        """Run one cooldown-protected comparison from a saved baseline."""
        if not _LIVE_SOURCE_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail={"code": "SOURCE_BUSY", "message": "已有职位抓取正在运行"},
            )
        try:
            with _repository(configured) as repository:
                check = JobRadarService(
                    repository, _discovery_service(repository, configured)
                ).check(
                    request.baseline_run_id,
                    cooldown_hours=configured.radar_min_interval_hours,
                    detail_limit=request.detail_top,
                    detail_cache_hours=configured.discovery_detail_cache_hours,
                    force=request.force,
                )
            return check.model_dump(mode="json")
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "RADAR_CHECK_FAILED", exc) from exc
        finally:
            _LIVE_SOURCE_LOCK.release()

    @application.get("/api/radar/checks")
    def radar_checks(
        candidate_id: str | None = None, limit: int = Query(default=20, ge=1, le=200)
    ) -> list[dict[str, object]]:
        """List persisted radar comparisons."""
        with _repository(configured) as repository:
            return [
                item.model_dump(mode="json")
                for item in repository.list_radar_checks(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/radar/checks/{run_id}")
    def radar_check(run_id: str) -> object:
        """Return one persisted radar comparison."""
        try:
            with _repository(configured) as repository:
                return repository.get_radar_check(run_id).model_dump(mode="json")
        except JobIntelError as exc:
            raise _error(404, "RADAR_NOT_FOUND", exc) from exc

    return application


app = create_app()
