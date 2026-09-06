"""FastAPI composition root for the local JobIntel web workspace."""

from __future__ import annotations

import re
import secrets
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from jobintel.config import JobIntelProviderName, JobIntelSettings, load_jobintel_settings
from jobintel.discovery.connectors.cdp import cdp_reachable
from jobintel.discovery.connectors.registry import build_connectors
from jobintel.discovery.models import (
    CompanySize,
    DiscoveryMode,
    EmploymentType,
    JobSearchPreference,
    JobSource,
)
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import EntityNotFoundError, IdempotencyConflictError, JobIntelError
from jobintel.models import FrozenDomainModel, JobAnalysis, NonEmptyStr
from jobintel.notifications.address import mask_email_address
from jobintel.notifications.factory import build_email_sender
from jobintel.notifications.models import CandidateEmailPreference, SMTPTransport
from jobintel.notifications.service import (
    DiscoveryEmailNotificationService,
    EmailNotificationError,
)
from jobintel.outreach.models import OutreachDraft, OutreachTone
from jobintel.outreach.policy import BOSS_DRAFT_POLICY
from jobintel.outreach.service import OutreachGenerationError, OutreachService
from jobintel.outreach.state import OutreachStateTransitionError
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
from jobintel.web.auth import (
    AuthenticationError,
    BootstrapClosedError,
    CreatedWebSession,
    LoginRateLimitError,
    LoginThrottle,
    ManagedWebUser,
    WebAuthStore,
    WebRole,
    WebSession,
    WebUser,
)
from jobintel.web.runtime_config import RuntimeConfigStore, safe_runtime_payload

_STATIC_DIR = Path(__file__).with_name("static")
_PREVIEW_ID = re.compile(r"web-[0-9a-f]{32}\.json")
# Chrome exposes one user-controlled BOSS session. Only source navigation is serialized;
# profile, analysis, account, email, and read APIs remain independently concurrent.
_BOSS_SOURCE_LOCK = threading.Lock()
_SESSION_COOKIE = "jobintel_session"
_PUBLIC_AUTH_PATHS = frozenset(
    {
        "/api/auth/status",
        "/api/auth/bootstrap",
        "/api/auth/login",
        "/api/auth/register",
    }
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_STATIC_PATHS = frozenset({"/static/app.css", "/static/login.js"})
_CANDIDATE_API_PREFIXES = (
    "/api/dashboard",
    "/api/profiles",
    "/api/discoveries",
    "/api/analyses",
    "/api/outreach-drafts",
    "/api/radar",
)


class DiscoveryRequest(FrozenDomainModel):
    """Browser-submitted discovery form after strict API validation."""

    candidate_id: NonEmptyStr
    profile_version: int | None = Field(default=None, ge=1)
    query: NonEmptyStr = Field(default="Agent开发", max_length=100)
    city: str = Field(default="北京", max_length=50)
    salary_min_k: int | None = Field(default=None, ge=0, le=1000)
    salary_max_k: int | None = Field(default=None, ge=0, le=1000)
    daily_salary_min_yuan: int | None = Field(default=None, ge=0, le=10000)
    daily_salary_max_yuan: int | None = Field(default=None, ge=0, le=10000)
    employment_types: tuple[EmploymentType, ...] = (EmploymentType.INTERNSHIP,)
    company_sizes: tuple[CompanySize, ...] = ()
    education_requirements: tuple[NonEmptyStr, ...] = ()
    experience_requirements: tuple[NonEmptyStr, ...] = ()
    exclusions: tuple[NonEmptyStr, ...] = ()
    exclude_outsourcing: bool = False
    exclude_training: bool = False
    exclude_agency: bool = False
    smart_expand: bool = False
    strict_salary: bool = False
    discovery_mode: DiscoveryMode = DiscoveryMode.HYBRID
    prefer_new: bool = True
    only_new: bool = False
    limit: int = Field(default=10, ge=1, le=500)
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


class GenerateOutreachRequest(FrozenDomainModel):
    """Options for one evidence-grounded outreach generation run."""

    tone: OutreachTone = OutreachTone.PROFESSIONAL
    focus_requirement_ids: tuple[NonEmptyStr, ...] = Field(default=(), max_length=3)
    provider: JobIntelProviderName | None = None


class ReviseOutreachRequest(FrozenDomainModel):
    """User-authored replacement text for an exact draft revision."""

    revision: int = Field(ge=1)
    message: NonEmptyStr = Field(max_length=BOSS_DRAFT_POLICY.max_message_chars)


class OutreachActionRequest(FrozenDomainModel):
    """One explicit user action against an exact draft revision."""

    revision: int = Field(ge=1)


class EmailDiscoveryRequest(FrozenDomainModel):
    """Bounded options for emailing a saved discovery batch."""

    limit: int = Field(default=50, ge=1, le=500)


class CandidateEmailPreferenceRequest(FrozenDomainModel):
    """Candidate-scoped recipient submitted from the settings form."""

    recipient_email: NonEmptyStr = Field(max_length=320)


class WebCredentialRequest(FrozenDomainModel):
    """Bounded credentials accepted by initial setup and login."""

    username: NonEmptyStr = Field(max_length=50)
    password: str = Field(min_length=10, max_length=128)


class RegisterCandidateRequest(WebCredentialRequest):
    """Candidate-provided information for self-service registration."""

    display_name: NonEmptyStr = Field(max_length=80)
    email: NonEmptyStr = Field(max_length=320)


class UpdateUserProfileRequest(FrozenDomainModel):
    """Editable account information for oneself or an administered candidate."""

    display_name: NonEmptyStr = Field(max_length=80)
    email: NonEmptyStr = Field(max_length=320)


class UserStatusRequest(FrozenDomainModel):
    """Administrator-controlled candidate account status."""

    is_active: bool


class PasswordResetRequest(FrozenDomainModel):
    """One bounded replacement password."""

    password: str = Field(min_length=10, max_length=128)


class ChangePasswordRequest(FrozenDomainModel):
    """Authenticated self-service password change."""

    current_password: str = Field(min_length=10, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class RuntimeConfigRequest(FrozenDomainModel):
    """Allow-listed administrator updates for live service configuration."""

    llm_provider: JobIntelProviderName | None = None
    anthropic_api_key: str | None = Field(default=None, max_length=500)
    anthropic_model: str | None = Field(default=None, min_length=1, max_length=200)
    openai_api_key: str | None = Field(default=None, max_length=500)
    openai_model: str | None = Field(default=None, min_length=1, max_length=200)
    deepseek_api_key: str | None = Field(default=None, max_length=500)
    deepseek_base_url: str | None = Field(default=None, min_length=1, max_length=500)
    deepseek_model: str | None = Field(default=None, min_length=1, max_length=200)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_transport: SMTPTransport | None = None
    smtp_username: str | None = Field(default=None, max_length=320)
    smtp_password: str | None = Field(default=None, max_length=500)
    smtp_from_address: str | None = Field(default=None, max_length=320)
    smtp_timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    discovery_cdp_port: int | None = Field(default=None, ge=1, le=65535)
    discovery_search_min_delay_seconds: float | None = Field(default=None, ge=1, le=30)
    discovery_search_max_delay_seconds: float | None = Field(default=None, ge=1, le=30)
    discovery_detail_min_delay_seconds: float | None = Field(default=None, ge=2, le=60)
    discovery_detail_max_delay_seconds: float | None = Field(default=None, ge=2, le=60)
    discovery_detail_cache_hours: int | None = Field(default=None, ge=1, le=168)
    radar_min_interval_hours: int | None = Field(default=None, ge=1, le=168)


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


@contextmanager
def _auth_store(settings: JobIntelSettings) -> Iterator[WebAuthStore]:
    """Open the migrated authentication store for one bounded operation."""
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        yield WebAuthStore(database, session_hours=settings.web_session_hours)
    finally:
        database.close()


def _runtime_settings(settings: JobIntelSettings) -> JobIntelSettings:
    """Resolve administrator-managed overrides for one operation."""
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        return RuntimeConfigStore(database, settings).resolve()
    finally:
        database.close()


def _auth_payload(session: WebSession, *, setup_required: bool = False) -> dict[str, object]:
    """Return only browser-safe session details."""
    return {
        "authenticated": True,
        "setup_required": setup_required,
        "user": {
            "user_id": session.user.user_id,
            "username": session.user.username,
            "display_name": session.user.display_name,
            "email": session.user.email,
            "role": session.user.role.value,
            "candidate_id": session.user.candidate_id,
        },
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at.isoformat(),
    }


def _set_session_cookie(
    response: Response,
    created: CreatedWebSession,
    settings: JobIntelSettings,
) -> None:
    """Issue one host-only HttpOnly session cookie."""
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=created.cookie_token,
        max_age=settings.web_session_hours * 60 * 60,
        path="/",
        secure=settings.web_cookie_secure,
        httponly=True,
        samesite="lax",
    )


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


def _current_user(request: Request) -> WebUser:
    """Return the identity established by the authentication middleware."""
    session: WebSession | None = request.state.web_session
    if session is None:  # Defensive; the middleware normally intercepts this path.
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED"})
    return session.user


def _require_admin(request: Request) -> WebUser:
    """Require the global administrator role for project-management APIs."""
    user = _current_user(request)
    if user.role is not WebRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_REQUIRED", "message": "该操作仅限管理员"},
        )
    return user


def _require_candidate(request: Request) -> WebUser:
    """Require the candidate role for every job-seeking business operation."""
    user = _current_user(request)
    if user.role is not WebRole.CANDIDATE:
        raise HTTPException(
            status_code=403,
            detail={"code": "CANDIDATE_REQUIRED", "message": "管理员不能使用候选人业务功能"},
        )
    return user


def _candidate_scope(request: Request, requested: str | None = None) -> str | None:
    """Resolve an optional candidate filter inside the caller's authorized scope."""
    user = _require_candidate(request)
    if user.candidate_id is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "CANDIDATE_NOT_BOUND", "message": "账户尚未绑定候选人档案"},
        )
    if requested is not None and requested != user.candidate_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "CANDIDATE_SCOPE_DENIED", "message": "不能访问其他候选人的数据"},
        )
    return user.candidate_id


def _require_candidate_access(request: Request, candidate_id: str) -> None:
    """Reject direct and indirect access outside a candidate account's scope."""
    scoped = _candidate_scope(request, candidate_id)
    if scoped is not None and scoped != candidate_id:  # Defensive for future role additions.
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})


def _user_payload(user: WebUser) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role.value,
        "candidate_id": user.candidate_id,
        "is_active": user.is_active,
    }


def _managed_user_payload(record: ManagedWebUser) -> dict[str, object]:
    return {
        **_user_payload(record.user),
        "created_at": record.created_at.isoformat(),
        "last_login_at": (
            record.last_login_at.isoformat() if record.last_login_at is not None else None
        ),
    }


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


def _outreach_payload(
    repository: SQLiteJobRepository,
    draft: OutreachDraft,
    *,
    include_events: bool = True,
) -> dict[str, object]:
    """Enrich a draft with the exact human-readable requirements and evidence."""
    job = repository.get_job(draft.job_id, draft.job_version)
    profile = repository.get_candidate_profile(draft.candidate_id, draft.profile_version)
    analysis = repository.get_analysis(draft.analysis_id)
    requirements = {item.requirement_id: item for item in job.requirements}
    evidence = {item.evidence_id: item for item in profile.evidence}
    matches = {item.requirement_id: item for item in analysis.requirement_matches}
    citations = []
    for claim in draft.claims:
        citations.append(
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "requirements": [
                    {
                        **requirements[requirement_id].model_dump(mode="json"),
                        "match_status": matches[requirement_id].status.value,
                    }
                    for requirement_id in claim.requirement_ids
                ],
                "evidence": [
                    evidence[evidence_id].model_dump(mode="json")
                    for evidence_id in claim.evidence_ids
                ],
            }
        )
    payload: dict[str, object] = {
        "outreach": {
            **draft.model_dump(mode="json"),
            "effective_message": draft.effective_message,
            "is_user_edited": draft.is_user_edited,
            "character_count": len(draft.effective_message),
        },
        "job": {
            "title": job.title,
            "company_name": job.company_name,
            "source_url": job.source_url,
        },
        "citations": citations,
    }
    if include_events:
        payload["events"] = [
            item.model_dump(mode="json")
            for item in repository.list_outreach_events(draft.outreach_id)
        ]
    return payload


def create_app(settings: JobIntelSettings | None = None) -> FastAPI:
    """Create the authenticated JobIntel web application."""
    configured = settings or load_jobintel_settings()
    login_throttle = LoginThrottle()
    application = FastAPI(
        title="JobIntel 工作台",
        version="1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    application.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @application.middleware("http")
    async def require_web_authentication(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Protect every workspace route and enforce CSRF on state changes."""
        path = request.url.path
        if path in _PUBLIC_STATIC_PATHS or path == "/login":
            response = await call_next(request)
        else:
            cookie_token = request.cookies.get(_SESSION_COOKIE)
            with _auth_store(configured) as auth:
                session = auth.resolve(cookie_token)
            request.state.web_session = session
            is_public_auth = path in _PUBLIC_AUTH_PATHS
            if session is None and not is_public_auth:
                if path.startswith("/api/"):
                    response = JSONResponse(
                        status_code=401,
                        content={
                            "detail": {
                                "code": "AUTHENTICATION_REQUIRED",
                                "message": "请先登录后再操作",
                            }
                        },
                    )
                else:
                    response = RedirectResponse(url="/login", status_code=303)
            elif (
                session is not None
                and path.startswith("/api/admin/")
                and session.user.role is not WebRole.ADMIN
            ):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "ADMIN_REQUIRED",
                            "message": "该操作仅限管理员",
                        }
                    },
                )
            elif (
                session is not None
                and any(path.startswith(prefix) for prefix in _CANDIDATE_API_PREFIXES)
                and session.user.role is not WebRole.CANDIDATE
            ):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "CANDIDATE_REQUIRED",
                            "message": "管理员不能使用候选人业务功能",
                        }
                    },
                )
            elif (
                session is not None
                and request.method not in _SAFE_METHODS
                and path not in _PUBLIC_AUTH_PATHS
                and not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), session.csrf_token
                )
            ):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "CSRF_VALIDATION_FAILED",
                            "message": "页面安全令牌已失效, 请刷新后重试",
                        }
                    },
                )
            else:
                response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        if path.startswith(("/api/", "/static/")) or path in {"/", "/login"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        """Serve first-user setup and returning-user login."""
        return FileResponse(_STATIC_DIR / "login.html")

    @application.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, object]:
        """Report setup and current-session state without exposing secrets."""
        session: WebSession | None = request.state.web_session
        with _auth_store(configured) as auth:
            setup_required = not auth.has_users()
        if session is None:
            return {
                "authenticated": False,
                "setup_required": setup_required,
                "user": None,
                "csrf_token": None,
                "expires_at": None,
            }
        return _auth_payload(session, setup_required=setup_required)

    @application.post("/api/auth/bootstrap")
    def bootstrap_auth(request: WebCredentialRequest) -> Response:
        """Create the only initial administrator when no account exists."""
        try:
            with _auth_store(configured) as auth:
                created = auth.bootstrap(request.username, request.password)
        except BootstrapClosedError as exc:
            raise _error(409, "AUTH_SETUP_ALREADY_COMPLETED", exc) from exc
        except ValueError as exc:
            raise _error(422, "INVALID_CREDENTIALS", exc) from exc
        response = JSONResponse(_auth_payload(created.session))
        _set_session_cookie(response, created, configured)
        return response

    @application.post("/api/auth/login")
    def login_auth(request: Request, credentials: WebCredentialRequest) -> Response:
        """Authenticate a user and issue a rotated opaque session cookie."""
        client_key = request.client.host if request.client is not None else "unknown"
        try:
            login_throttle.check(client_key)
        except LoginRateLimitError as exc:
            raise HTTPException(
                status_code=429,
                detail={"code": "LOGIN_RATE_LIMITED", "message": str(exc)},
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        try:
            with _auth_store(configured) as auth:
                created = auth.login(credentials.username, credentials.password)
                auth.logout(request.cookies.get(_SESSION_COOKIE))
        except (AuthenticationError, ValueError) as exc:
            login_throttle.failed(client_key)
            generic = AuthenticationError("用户名或密码错误")
            raise _error(401, "INVALID_LOGIN", generic) from exc
        login_throttle.succeeded(client_key)
        response = JSONResponse(_auth_payload(created.session))
        _set_session_cookie(response, created, configured)
        return response

    @application.post("/api/auth/register")
    def register_candidate(account: RegisterCandidateRequest) -> Response:
        """Register and sign in one candidate with a server-issued data scope."""
        try:
            with _auth_store(configured) as auth:
                if not auth.has_users():
                    raise ValueError("请先由项目管理员完成系统初始化")
                created = auth.register_candidate(
                    account.username,
                    account.password,
                    account.display_name,
                    account.email,
                )
        except ValueError as exc:
            raise _error(409, "REGISTRATION_FAILED", exc) from exc
        response = JSONResponse(_auth_payload(created.session))
        _set_session_cookie(response, created, configured)
        return response

    @application.post("/api/auth/logout")
    def logout_auth(request: Request) -> Response:
        """Revoke the active session and expire its browser cookie."""
        with _auth_store(configured) as auth:
            auth.logout(request.cookies.get(_SESSION_COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            key=_SESSION_COOKIE,
            path="/",
            secure=configured.web_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @application.post("/api/auth/change-password")
    def change_password(request: Request, password: ChangePasswordRequest) -> Response:
        """Change the current user's password and rotate all active sessions."""
        user = _current_user(request)
        try:
            with _auth_store(configured) as auth:
                created = auth.change_password(
                    user.user_id,
                    password.current_password,
                    password.new_password,
                )
        except (AuthenticationError, ValueError) as exc:
            raise _error(400, "PASSWORD_CHANGE_FAILED", exc) from exc
        response = JSONResponse(_auth_payload(created.session))
        _set_session_cookie(response, created, configured)
        return response

    @application.patch("/api/auth/profile")
    def update_own_profile(
        request: Request, profile: UpdateUserProfileRequest
    ) -> dict[str, object]:
        """Allow an authenticated user to update their own account information."""
        user = _current_user(request)
        try:
            with _auth_store(configured) as auth:
                return _user_payload(
                    auth.update_user_profile(user.user_id, profile.display_name, profile.email)
                )
        except (AuthenticationError, ValueError) as exc:
            raise _error(400, "USER_PROFILE_UPDATE_FAILED", exc) from exc

    @application.get("/api/admin/users")
    def admin_users(request: Request) -> list[dict[str, object]]:
        """List registered accounts with candidate profile readiness metadata."""
        _require_admin(request)
        with _auth_store(configured) as auth:
            accounts = auth.list_users()
        with _repository(configured) as repository:
            profiles = {
                profile.candidate_id: profile for profile in repository.list_candidate_profiles()
            }
        records: list[dict[str, object]] = []
        for account in accounts:
            candidate_id = account.user.candidate_id
            profile = profiles.get(candidate_id) if candidate_id is not None else None
            records.append(
                {
                    **_managed_user_payload(account),
                    "account_status": "active" if account.user.is_active else "disabled",
                    "profile_exists": profile is not None,
                    "profile_version": profile.profile_version if profile is not None else None,
                }
            )
        return sorted(
            records,
            key=lambda item: (
                item["role"] != WebRole.ADMIN.value,
                str(item["candidate_id"] or ""),
                str(item["username"] or ""),
            ),
        )

    @application.patch("/api/admin/users/{user_id}")
    def update_candidate_user(
        user_id: str,
        request: Request,
        profile: UpdateUserProfileRequest,
    ) -> dict[str, object]:
        """Update one registered candidate's editable account information."""
        _require_admin(request)
        try:
            with _auth_store(configured) as auth:
                target = auth.get_user(user_id)
                if target.role is not WebRole.CANDIDATE:
                    raise ValueError("不能通过候选人管理接口修改管理员")
                return _user_payload(
                    auth.update_user_profile(user_id, profile.display_name, profile.email)
                )
        except AuthenticationError as exc:
            raise _error(404, "USER_NOT_FOUND", exc) from exc
        except ValueError as exc:
            raise _error(400, "USER_PROFILE_UPDATE_FAILED", exc) from exc

    @application.patch("/api/admin/users/{user_id}/status")
    def set_candidate_user_status(
        user_id: str, request: Request, status: UserStatusRequest
    ) -> dict[str, object]:
        """Activate or deactivate one candidate account."""
        _require_admin(request)
        try:
            with _auth_store(configured) as auth:
                return _user_payload(auth.set_candidate_user_active(user_id, status.is_active))
        except AuthenticationError as exc:
            raise _error(404, "USER_NOT_FOUND", exc) from exc
        except ValueError as exc:
            raise _error(400, "USER_STATUS_FAILED", exc) from exc

    @application.post("/api/admin/users/{user_id}/reset-password")
    def reset_candidate_user_password(
        user_id: str, request: Request, password: PasswordResetRequest
    ) -> dict[str, object]:
        """Reset a candidate password and revoke all of that account's sessions."""
        _require_admin(request)
        try:
            with _auth_store(configured) as auth:
                return _user_payload(auth.reset_candidate_password(user_id, password.password))
        except AuthenticationError as exc:
            raise _error(404, "USER_NOT_FOUND", exc) from exc
        except ValueError as exc:
            raise _error(400, "USER_PASSWORD_RESET_FAILED", exc) from exc

    @application.get("/api/admin/environment")
    def admin_environment(request: Request) -> dict[str, object]:
        """Return editable runtime configuration without returning secret values."""
        _require_admin(request)
        settings_now = _runtime_settings(configured)
        return {
            **safe_runtime_payload(settings_now),
            "boss_browser_ready": cdp_reachable(settings_now.discovery_cdp_port),
        }

    @application.put("/api/admin/environment")
    def update_admin_environment(
        request: Request, update: RuntimeConfigRequest
    ) -> dict[str, object]:
        """Validate and persist live configuration changes made by an administrator."""
        admin = _require_admin(request)
        values = {
            key: value
            for key, value in update.model_dump(exclude_unset=True).items()
            if value is not None
        }
        database = JobIntelDatabase.connect(configured.jobintel_db_path)
        try:
            MigrationRunner(database).migrate()
            settings_now = RuntimeConfigStore(database, configured).update(
                values, updated_by=admin.user_id
            )
        except ValueError as exc:
            raise _error(422, "ENVIRONMENT_CONFIG_INVALID", exc) from exc
        finally:
            database.close()
        return {
            **safe_runtime_payload(settings_now),
            "boss_browser_ready": cdp_reachable(settings_now.discovery_cdp_port),
        }

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve the single-page workspace shell."""
        return FileResponse(_STATIC_DIR / "index.html")

    @application.get("/api/health")
    def health() -> dict[str, object]:
        """Report local prerequisites without contacting BOSS."""
        settings_now = _runtime_settings(configured)
        return {
            "ok": True,
            "boss_browser_ready": cdp_reachable(settings_now.discovery_cdp_port),
            "provider": settings_now.llm_provider.value,
            "radar_interval_hours": settings_now.radar_min_interval_hours,
            "smtp_notification_ready": settings_now.smtp_notification_ready,
        }

    @application.get("/api/dashboard")
    def dashboard(request: Request) -> dict[str, object]:
        """Return compact data for the landing dashboard."""
        candidate_id = _candidate_scope(request)
        with _repository(configured) as repository:
            if candidate_id is None:
                profiles = repository.list_candidate_profiles()
            else:
                try:
                    profiles = (repository.get_candidate_profile(candidate_id),)
                except EntityNotFoundError:
                    profiles = ()
            discoveries = repository.list_discovery_runs(candidate_id=candidate_id, limit=8)
            analyses = repository.list_analyses(candidate_id=candidate_id, limit=8)
            radar_checks = repository.list_radar_checks(candidate_id=candidate_id, limit=8)
            profile_payloads = []
            for profile in profiles:
                preference = repository.find_candidate_email_preference(profile.candidate_id)
                profile_payloads.append(
                    {
                        "candidate_id": profile.candidate_id,
                        "profile_version": profile.profile_version,
                        "summary": profile.summary,
                        "evidence_count": len(profile.evidence),
                        "created_at": profile.created_at,
                        "email_notification_configured": preference is not None,
                        "recipient_masked": (
                            mask_email_address(preference.recipient_email)
                            if preference is not None
                            else None
                        ),
                    }
                )
            return {
                "counts": repository.dashboard_counts(candidate_id=candidate_id),
                "profiles": profile_payloads,
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
    def profiles(request: Request) -> list[dict[str, object]]:
        """List latest candidate profiles."""
        candidate_id = _candidate_scope(request)
        with _repository(configured) as repository:
            if candidate_id is None:
                items = repository.list_candidate_profiles()
            else:
                try:
                    items = (repository.get_candidate_profile(candidate_id),)
                except EntityNotFoundError:
                    items = ()
            return [item.model_dump(mode="json") for item in items]

    @application.get("/api/profiles/{candidate_id}")
    def profile(
        candidate_id: str,
        request: Request,
        version: int | None = Query(default=None, ge=1),
    ) -> object:
        """Return one complete candidate profile."""
        _require_candidate_access(request, candidate_id)
        try:
            with _repository(configured) as repository:
                return repository.get_candidate_profile(candidate_id, version).model_dump(
                    mode="json"
                )
        except JobIntelError as exc:
            raise _error(404, "PROFILE_NOT_FOUND", exc) from exc

    @application.get("/api/profiles/{candidate_id}/notification-email")
    def candidate_notification_email(candidate_id: str, request: Request) -> dict[str, object]:
        """Return only the masked recipient setting for one candidate."""
        _require_candidate_access(request, candidate_id)
        try:
            with _repository(configured) as repository:
                repository.get_candidate_profile(candidate_id)
                preference = repository.find_candidate_email_preference(candidate_id)
            return {
                "candidate_id": candidate_id,
                "configured": preference is not None,
                "recipient_masked": (
                    mask_email_address(preference.recipient_email) if preference else None
                ),
            }
        except EntityNotFoundError as exc:
            raise _error(404, "PROFILE_NOT_FOUND", exc) from exc

    @application.put("/api/profiles/{candidate_id}/notification-email")
    def save_candidate_notification_email(
        candidate_id: str,
        http_request: Request,
        request: CandidateEmailPreferenceRequest,
    ) -> dict[str, object]:
        """Create or replace one candidate's persisted notification recipient."""
        _require_candidate_access(http_request, candidate_id)
        try:
            with _repository(configured) as repository:
                existing = repository.find_candidate_email_preference(candidate_id)
                now = datetime.now(UTC)
                saved = repository.save_candidate_email_preference(
                    CandidateEmailPreference(
                        candidate_id=candidate_id,
                        recipient_email=request.recipient_email,
                        created_at=existing.created_at if existing else now,
                        updated_at=now,
                    )
                )
            return {
                "candidate_id": saved.candidate_id,
                "configured": True,
                "recipient_masked": mask_email_address(saved.recipient_email),
                "updated_at": saved.updated_at,
            }
        except EntityNotFoundError as exc:
            raise _error(404, "PROFILE_NOT_FOUND", exc) from exc
        except ValueError as exc:
            raise _error(422, "INVALID_NOTIFICATION_EMAIL", exc) from exc

    @application.post("/api/profiles/preview")
    async def preview_profile(
        request: Request,
        candidate_id: Annotated[str, Form()],
        resume: Annotated[UploadFile, File()],
        provider: Annotated[JobIntelProviderName | None, Form()] = None,
    ) -> dict[str, object]:
        """Parse an uploaded resume and retain a private, reviewable preview."""
        candidate_id = candidate_id.strip()
        _require_candidate_access(request, candidate_id)
        if not candidate_id:
            raise _error(422, "INVALID_CANDIDATE", ValueError("candidate_id is required"))
        filename = Path(resume.filename or "resume.txt").name
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".pdf", ".txt", ".md", ".markdown"}:
            raise _error(422, "UNSUPPORTED_RESUME", ValueError("仅支持 PDF、TXT、Markdown 简历"))
        content = await resume.read(MAX_RESUME_BYTES + 1)
        if len(content) > MAX_RESUME_BYTES:
            raise _error(413, "RESUME_TOO_LARGE", ValueError("简历文件不能超过 10 MiB"))
        request_settings = _with_provider(_runtime_settings(configured), provider)
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
    def confirm_profile(http_request: Request, request: ConfirmProfileRequest) -> object:
        """Atomically append a reviewed server-owned preview as a profile version."""
        try:
            path = _preview_path(configured, request.preview_id)
            preview = CandidateProfilePreview.model_validate_json(path.read_text(encoding="utf-8"))
            _require_candidate_access(http_request, preview.candidate_id)
            with _repository(configured) as repository:
                saved = repository.save_candidate_profile(materialize_profile(preview))
            return saved.model_dump(mode="json")
        except FileNotFoundError as exc:
            raise _error(404, "PROFILE_PREVIEW_NOT_FOUND", exc) from exc
        except (JobIntelError, OSError, ValueError) as exc:
            raise _error(400, "PROFILE_CONFIRM_FAILED", exc) from exc

    @application.post("/api/discoveries")
    def discover(http_request: Request, request: DiscoveryRequest) -> dict[str, object]:
        """Run one bounded BOSS discovery using the existing protected connector."""
        _require_candidate_access(http_request, request.candidate_id)
        settings_now = _runtime_settings(configured)
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
                company_sizes=request.company_sizes,
                education_requirements=request.education_requirements,
                experience_requirements=request.experience_requirements,
                exclusions=_exclusions(request),
                smart_expand=request.smart_expand,
                discovery_mode=request.discovery_mode,
                prefer_new=request.prefer_new,
                only_new=request.only_new,
                sources=(JobSource.BOSS,),
                limit=request.limit,
            )
            if not _BOSS_SOURCE_LOCK.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SOURCE_BUSY",
                        "message": "BOSS 采集通道正在使用中; 其他功能仍可正常操作",
                    },
                )
            try:
                with _repository(settings_now) as repository:
                    run = _discovery_service(repository, settings_now).discover(
                        preference,
                        detail_limit=request.detail_top,
                        detail_cache_hours=settings_now.discovery_detail_cache_hours,
                    )
            finally:
                _BOSS_SOURCE_LOCK.release()
            return {"discovery": run.model_dump(mode="json")}
        except HTTPException:
            raise
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "DISCOVERY_FAILED", exc) from exc

    @application.get("/api/discoveries")
    def discoveries(
        request: Request,
        candidate_id: str | None = None,
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict[str, object]]:
        """List saved discovery runs."""
        candidate_id = _candidate_scope(request, candidate_id)
        with _repository(configured) as repository:
            return [
                item.model_dump(mode="json")
                for item in repository.list_discovery_runs(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/discoveries/{run_id}")
    def discovery(run_id: str, request: Request) -> object:
        """Return one complete saved discovery run."""
        try:
            with _repository(configured) as repository:
                run = repository.get_discovery_run(run_id)
                _require_candidate_access(request, run.preference.candidate_id)
                return run.model_dump(mode="json")
        except JobIntelError as exc:
            raise _error(404, "DISCOVERY_NOT_FOUND", exc) from exc

    @application.post("/api/discoveries/{run_id}/notifications/email")
    def email_discovery(
        run_id: str, http_request: Request, request: EmailDiscoveryRequest
    ) -> object:
        """Email a saved result batch to its candidate's persisted recipient."""
        settings_now = _runtime_settings(configured)
        try:
            with _repository(configured) as repository:
                run = repository.get_discovery_run(run_id)
                _require_candidate_access(http_request, run.preference.candidate_id)
                try:
                    preference = repository.get_candidate_email_preference(
                        run.preference.candidate_id
                    )
                except EntityNotFoundError as exc:
                    raise _error(409, "EMAIL_RECIPIENT_NOT_CONFIGURED", exc) from exc
                sender = build_email_sender(settings_now, recipient=preference.recipient_email)
                receipt = DiscoveryEmailNotificationService(repository, sender).send_discovery(
                    run_id, limit=request.limit
                )
            return receipt.model_dump(mode="json")
        except EntityNotFoundError as exc:
            raise _error(404, "DISCOVERY_NOT_FOUND", exc) from exc
        except (EmailNotificationError, RuntimeError, ValueError) as exc:
            raise _error(400, "EMAIL_NOTIFICATION_FAILED", exc) from exc

    @application.post("/api/discoveries/{run_id}/analyze")
    def analyze_discovery(
        run_id: str, http_request: Request, request: AnalyzeDiscoveryRequest
    ) -> object:
        """Analyze saved jobs without touching BOSS again."""
        request_settings = _with_provider(_runtime_settings(configured), request.provider)
        try:
            with _repository(request_settings) as repository:
                run = repository.get_discovery_run(run_id)
                _require_candidate_access(http_request, run.preference.candidate_id)
                llm = build_jobintel_provider(request_settings)
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
        request: Request,
        candidate_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, object]]:
        """List complete persisted analyses."""
        candidate_id = _candidate_scope(request, candidate_id)
        with _repository(configured) as repository:
            return [
                _analysis_payload(repository, item)
                for item in repository.list_analyses(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/analyses/{analysis_id}")
    def analysis(analysis_id: str, request: Request) -> object:
        """Return one complete persisted analysis."""
        try:
            with _repository(configured) as repository:
                item = repository.get_analysis(analysis_id)
                _require_candidate_access(request, item.candidate_id)
                return _analysis_payload(repository, item)
        except JobIntelError as exc:
            raise _error(404, "ANALYSIS_NOT_FOUND", exc) from exc

    @application.post("/api/analyses/{analysis_id}/outreach-drafts")
    async def generate_outreach(
        analysis_id: str,
        http_request: Request,
        request: GenerateOutreachRequest,
    ) -> dict[str, object]:
        """Generate and persist one guarded outreach draft without contacting BOSS."""
        request_settings = _with_provider(_runtime_settings(configured), request.provider)
        try:
            with _repository(request_settings) as repository:
                analysis = repository.get_analysis(analysis_id)
                _require_candidate_access(http_request, analysis.candidate_id)
                llm = build_jobintel_provider(request_settings)
                result = await OutreachService(
                    llm,
                    repository,
                    max_repairs=request_settings.outreach_max_repairs,
                ).generate(
                    analysis_id=analysis_id,
                    tone=request.tone,
                    focus_requirement_ids=request.focus_requirement_ids,
                )
                payload = _outreach_payload(repository, result.outreach)
                payload["telemetry"] = result.telemetry.model_dump(mode="json")
                return payload
        except EntityNotFoundError as exc:
            raise _error(404, "ANALYSIS_NOT_FOUND", exc) from exc
        except OutreachGenerationError as exc:
            detail = ValueError(
                str(exc)
                + (
                    ": " + ", ".join(item.code.value for item in exc.violations)
                    if exc.violations
                    else ""
                )
            )
            raise _error(400, "OUTREACH_GENERATION_FAILED", detail) from exc
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "OUTREACH_GENERATION_FAILED", exc) from exc

    @application.get("/api/outreach-drafts")
    def outreach_drafts(
        request: Request,
        analysis_id: str | None = None,
        candidate_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, object]]:
        """List latest outreach revisions with display-safe citation context."""
        try:
            with _repository(configured) as repository:
                if analysis_id is not None:
                    analysis = repository.get_analysis(analysis_id)
                    _require_candidate_access(request, analysis.candidate_id)
                    drafts = repository.list_outreach_for_analysis(analysis_id)
                else:
                    candidate_id = _candidate_scope(request, candidate_id)
                    drafts = repository.list_outreach(candidate_id=candidate_id, limit=limit)
                return [
                    _outreach_payload(repository, item, include_events=False)
                    for item in drafts[:limit]
                ]
        except EntityNotFoundError as exc:
            raise _error(404, "ANALYSIS_NOT_FOUND", exc) from exc

    @application.get("/api/outreach-drafts/{outreach_id}")
    def outreach_draft(
        outreach_id: str,
        request: Request,
        revision: int | None = Query(default=None, ge=1),
    ) -> dict[str, object]:
        """Return one complete outreach revision and its audit history."""
        try:
            with _repository(configured) as repository:
                draft = repository.get_outreach(outreach_id, revision)
                _require_candidate_access(request, draft.candidate_id)
                return _outreach_payload(repository, draft)
        except EntityNotFoundError as exc:
            raise _error(404, "OUTREACH_NOT_FOUND", exc) from exc

    @application.post("/api/outreach-drafts/{outreach_id}/revisions")
    def revise_outreach(
        outreach_id: str,
        http_request: Request,
        request: ReviseOutreachRequest,
    ) -> dict[str, object]:
        """Create a new draft revision from explicitly user-edited text."""
        try:
            with _repository(configured) as repository:
                existing = repository.get_outreach(outreach_id)
                _require_candidate_access(http_request, existing.candidate_id)
                draft = OutreachService(None, repository).revise(
                    outreach_id,
                    request.message,
                    revision=request.revision,
                )
                return _outreach_payload(repository, draft)
        except EntityNotFoundError as exc:
            raise _error(404, "OUTREACH_NOT_FOUND", exc) from exc
        except IdempotencyConflictError as exc:
            raise _error(409, "OUTREACH_REVISION_CONFLICT", exc) from exc
        except (JobIntelError, ValueError) as exc:
            raise _error(400, "OUTREACH_REVISION_FAILED", exc) from exc

    def apply_outreach_action(
        outreach_id: str,
        http_request: Request,
        request: OutreachActionRequest,
        action: str,
    ) -> dict[str, object]:
        """Apply one local review action with shared conflict semantics."""
        try:
            with _repository(configured) as repository:
                existing = repository.get_outreach(outreach_id)
                _require_candidate_access(http_request, existing.candidate_id)
                service = OutreachService(None, repository)
                operation = {
                    "approve": service.approve,
                    "copied": service.record_copied,
                    "opened": service.record_opened,
                    "sent-confirmed": service.confirm_sent,
                    "dismiss": service.dismiss,
                }[action]
                draft = operation(outreach_id, revision=request.revision)
                return _outreach_payload(repository, draft)
        except EntityNotFoundError as exc:
            raise _error(404, "OUTREACH_NOT_FOUND", exc) from exc
        except (IdempotencyConflictError, OutreachStateTransitionError) as exc:
            raise _error(409, "OUTREACH_STATE_CONFLICT", exc) from exc
        except (JobIntelError, ValueError) as exc:
            raise _error(400, "OUTREACH_ACTION_FAILED", exc) from exc

    @application.post("/api/outreach-drafts/{outreach_id}/approve")
    def approve_outreach(
        outreach_id: str, http_request: Request, request: OutreachActionRequest
    ) -> dict[str, object]:
        """Approve an exact draft revision without sending it."""
        return apply_outreach_action(outreach_id, http_request, request, "approve")

    @application.post("/api/outreach-drafts/{outreach_id}/events/copied")
    def record_outreach_copied(
        outreach_id: str, http_request: Request, request: OutreachActionRequest
    ) -> dict[str, object]:
        """Record that the user copied an approved draft."""
        return apply_outreach_action(outreach_id, http_request, request, "copied")

    @application.post("/api/outreach-drafts/{outreach_id}/events/opened")
    def record_outreach_opened(
        outreach_id: str, http_request: Request, request: OutreachActionRequest
    ) -> dict[str, object]:
        """Record that the user opened the stored source URL."""
        return apply_outreach_action(outreach_id, http_request, request, "opened")

    @application.post("/api/outreach-drafts/{outreach_id}/events/sent-confirmed")
    def confirm_outreach_sent(
        outreach_id: str, http_request: Request, request: OutreachActionRequest
    ) -> dict[str, object]:
        """Record the user's confirmation of a manual platform send."""
        return apply_outreach_action(outreach_id, http_request, request, "sent-confirmed")

    @application.post("/api/outreach-drafts/{outreach_id}/dismiss")
    def dismiss_outreach(
        outreach_id: str, http_request: Request, request: OutreachActionRequest
    ) -> dict[str, object]:
        """Dismiss an exact draft revision."""
        return apply_outreach_action(outreach_id, http_request, request, "dismiss")

    @application.post("/api/radar/checks")
    def check_radar(http_request: Request, request: RadarRequest) -> object:
        """Run one cooldown-protected comparison from a saved baseline."""
        settings_now = _runtime_settings(configured)
        try:
            with _repository(configured) as repository:
                baseline = repository.get_discovery_run(request.baseline_run_id)
                _require_candidate_access(http_request, baseline.preference.candidate_id)
        except JobIntelError as exc:
            raise _error(404, "DISCOVERY_NOT_FOUND", exc) from exc
        if not _BOSS_SOURCE_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SOURCE_BUSY",
                    "message": "BOSS 采集通道正在使用中; 其他功能仍可正常操作",
                },
            )
        try:
            with _repository(settings_now) as repository:
                check = JobRadarService(
                    repository, _discovery_service(repository, settings_now)
                ).check(
                    request.baseline_run_id,
                    cooldown_hours=settings_now.radar_min_interval_hours,
                    detail_limit=request.detail_top,
                    detail_cache_hours=settings_now.discovery_detail_cache_hours,
                    force=request.force,
                )
            return check.model_dump(mode="json")
        except (JobIntelError, RuntimeError, ValueError) as exc:
            raise _error(400, "RADAR_CHECK_FAILED", exc) from exc
        finally:
            _BOSS_SOURCE_LOCK.release()

    @application.get("/api/radar/checks")
    def radar_checks(
        request: Request,
        candidate_id: str | None = None,
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict[str, object]]:
        """List persisted radar comparisons."""
        candidate_id = _candidate_scope(request, candidate_id)
        with _repository(configured) as repository:
            return [
                item.model_dump(mode="json")
                for item in repository.list_radar_checks(candidate_id=candidate_id, limit=limit)
            ]

    @application.get("/api/radar/checks/{run_id}")
    def radar_check(run_id: str, request: Request) -> object:
        """Return one persisted radar comparison."""
        try:
            with _repository(configured) as repository:
                check = repository.get_radar_check(run_id)
                run = repository.get_discovery_run(check.run_id)
                _require_candidate_access(request, run.preference.candidate_id)
                return check.model_dump(mode="json")
        except JobIntelError as exc:
            raise _error(404, "RADAR_NOT_FOUND", exc) from exc

    return application


app = create_app()
