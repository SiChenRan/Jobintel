"""Primary JobIntel command-line composition root and presentation layer."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jobintel.agent.core import JobIntelAgent, JobIntelAgentError, JobIntelAgentResult
from jobintel.config import (
    JobIntelProviderName,
    load_jobintel_settings,
)
from jobintel.discovery.connectors.cdp import cdp_reachable, setup_chrome
from jobintel.discovery.connectors.registry import build_connectors
from jobintel.discovery.models import (
    CompanySize,
    DiscoveryRun,
    EmploymentType,
    JobSearchPreference,
    JobSource,
    RadarCheck,
    RadarEventStatus,
)
from jobintel.discovery.service import JobDiscoveryService
from jobintel.errors import JobIntelError
from jobintel.models import CandidateProfile, JobAnalysis, JobPosting, MatchStatus, Recommendation
from jobintel.notifications.factory import build_email_sender
from jobintel.notifications.service import (
    DiscoveryEmailNotificationService,
    EmailNotificationError,
)
from jobintel.outreach.models import OutreachDraft, OutreachEventType, OutreachTone
from jobintel.outreach.service import OutreachGenerationError, OutreachService
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MigrationRunner
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database
from jobintel.providers.base import LLMProvider
from jobintel.providers.factory import build_jobintel_provider
from jobintel.services.discovery_analysis import (
    analyze_discovery_hits as _analyze_discovery_hits,
)
from jobintel.services.intake import AnalysisRequest
from jobintel.services.radar import JobRadarService
from jobintel.services.resume_parser import (
    CandidateProfilePreview,
    ResumeParserService,
    materialize_profile,
)

app = typer.Typer(
    name="jobintel",
    help="Evidence-grounded, deterministically scored job application analysis.",
    add_completion=False,
    no_args_is_help=True,
)
profile_app = typer.Typer(
    help="Import and inspect immutable, evidence-backed candidate profiles.",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")
radar_app = typer.Typer(
    help="Run low-frequency incremental checks from a saved discovery baseline.",
    no_args_is_help=True,
)
app.add_typer(radar_app, name="radar")
outreach_app = typer.Typer(
    help="生成、审核并记录基于证据的 HR 沟通草稿 (不会自动发送)。",
    no_args_is_help=True,
)
app.add_typer(outreach_app, name="outreach")
notify_app = typer.Typer(
    help="将已保存的职位搜索结果发送到配置的通知邮箱。",
    no_args_is_help=True,
)
app.add_typer(notify_app, name="notify")

_EXCLUSION_PRESETS = {
    "outsourcing": ("外包", "驻场"),
    "training": ("培训机构", "岗前培训", "付费培训", "实训"),
    "agency": ("猎头", "人力资源", "人才服务"),
}


def _console() -> Console:
    """Create a console bound to the current command stdout."""
    return Console()


def _ensure_seeded(database: JobIntelDatabase) -> None:
    """Migrate and seed a completely empty JobIntel database."""
    MigrationRunner(database).migrate()
    row = database.connection.execute(
        "SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM candidate_profiles)"
    ).fetchone()
    if int(row[0]) == 0 and int(row[1]) == 0:
        seed_database(database)


def _emit_json(value: object) -> None:
    """Write stable UTF-8 JSON to stdout."""
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _abort(
    console: Console,
    message: str,
    *,
    code: str,
    json_output: bool,
) -> NoReturn:
    """Render one safe CLI error and exit non-zero."""
    if json_output:
        _emit_json({"error": {"code": code, "message": message}})
    else:
        console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _default_profile_preview_path(
    database_path: Path, candidate_id: str, profile_version: int
) -> Path:
    """Choose a non-sensitive filename beside the configured database."""
    candidate_digest = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:12]
    return (
        database_path.parent
        / "profile-previews"
        / (f"candidate-{candidate_digest}-v{profile_version}.json")
    )


def _write_profile_preview(path: Path, preview: CandidateProfilePreview) -> None:
    """Write a private, validated preview artifact for explicit confirmation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preview.model_dump_json(indent=2), encoding="utf-8")
    path.chmod(0o600)


def _render_profile(console: Console, profile: CandidateProfile, *, title: str) -> None:
    """Render one candidate profile and its independently citable evidence."""
    console.print(
        Panel(
            profile.summary or "(未填写摘要)",
            title=f"{title} · {profile.candidate_id}@{profile.profile_version}",
            border_style="cyan",
        )
    )
    table = Table(title=f"候选人证据 ({len(profile.evidence)} 条)", expand=True)
    table.add_column("序号", justify="right")
    table.add_column("类型")
    table.add_column("标题")
    table.add_column("内容")
    table.add_column("技能")
    for item in profile.evidence:
        table.add_row(
            str(item.source_order + 1),
            item.evidence_type.value,
            item.title,
            item.content,
            "、".join(item.skills) or "—",
        )
    console.print(table)


def _render_outreach(console: Console, draft: OutreachDraft) -> None:
    """Render one reviewed outreach revision and its evidence references."""
    edited = " · 用户已修改" if draft.is_user_edited else ""
    console.print(
        Panel(
            draft.effective_message,
            title=(
                f"HR 沟通草稿 · {draft.outreach_id}@{draft.revision} · {draft.status.value}{edited}"
            ),
            border_style="cyan",
        )
    )
    citations = Table(title="事实声明与证据引用", expand=True)
    citations.add_column("声明")
    citations.add_column("岗位要求 ID")
    citations.add_column("简历证据 ID")
    for claim in draft.claims:
        citations.add_row(
            claim.text,
            "、".join(claim.requirement_ids),
            "、".join(claim.evidence_ids),
        )
    console.print(citations)
    console.print(
        "[yellow]安全提示: 这里只生成草稿。请人工核对、复制并在 BOSS 直聘中手动发送。[/yellow]"
    )


@profile_app.command("import")
def import_profile(
    candidate_id: str = typer.Option(..., "--candidate-id", "-c"),
    resume_file: Path = typer.Option(
        ...,
        "--resume-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    preview_file: Path | None = typer.Option(
        None,
        "--preview-file",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="Where to write the reviewable JSON preview.",
    ),
    provider: JobIntelProviderName | None = typer.Option(None, "--provider", "-p"),
    json_output: bool = typer.Option(False, "--json", help="Emit preview JSON."),
) -> None:
    """Parse a resume into a preview without changing candidate profile data."""
    settings = load_jobintel_settings()
    if provider is not None:
        settings.llm_provider = provider
    console = _console()
    try:
        llm = build_jobintel_provider(settings)
    except (ImportError, RuntimeError, ValueError) as exc:
        _abort(
            console,
            str(exc),
            code="PROFILE_PROVIDER_UNAVAILABLE",
            json_output=json_output,
        )

    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        version = repository.next_candidate_profile_version(candidate_id)
        service = ResumeParserService(llm, max_repairs=settings.parser_max_repairs)
        try:
            if json_output:
                result = asyncio.run(
                    service.preview(
                        candidate_id=candidate_id,
                        profile_version=version,
                        resume_file=resume_file,
                    )
                )
            else:
                with console.status(f"[bold]正在解析简历[/bold] {resume_file.name}…"):
                    result = asyncio.run(
                        service.preview(
                            candidate_id=candidate_id,
                            profile_version=version,
                            resume_file=resume_file,
                        )
                    )
            output_path = preview_file or _default_profile_preview_path(
                settings.jobintel_db_path, candidate_id, version
            )
            _write_profile_preview(output_path, result.preview)
        except (OSError, RuntimeError, ValueError) as exc:
            _abort(
                console,
                str(exc),
                code="PROFILE_IMPORT_FAILED",
                json_output=json_output,
            )
    finally:
        database.close()

    if json_output:
        _emit_json(
            {
                "preview_file": str(output_path),
                "preview": result.preview.model_dump(mode="json"),
                "telemetry": result.telemetry.model_dump(mode="json"),
                "persisted": False,
            }
        )
        return
    _render_profile(console, materialize_profile(result.preview), title="简历导入预览")
    console.print(f"[yellow]尚未入库。预览文件:[/yellow] {output_path}")
    console.print(f"确认无误后运行: [bold]jobintel profile confirm {output_path}[/bold]")


@profile_app.command("confirm")
def confirm_profile(
    preview_file: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit CandidateProfile JSON."),
) -> None:
    """Validate and atomically persist a previously reviewed profile preview."""
    settings = load_jobintel_settings()
    console = _console()
    try:
        preview = CandidateProfilePreview.model_validate_json(
            preview_file.read_text(encoding="utf-8")
        )
        profile = materialize_profile(preview)
    except (OSError, ValueError) as exc:
        _abort(
            console,
            f"无法读取合法的简历预览: {exc}",
            code="INVALID_PROFILE_PREVIEW",
            json_output=json_output,
        )

    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        try:
            saved = repository.save_candidate_profile(profile)
        except JobIntelError as exc:
            _abort(
                console,
                str(exc),
                code="PROFILE_CONFIRM_FAILED",
                json_output=json_output,
            )
    finally:
        database.close()

    if json_output:
        typer.echo(saved.model_dump_json(indent=2))
    else:
        _render_profile(console, saved, title="已保存候选人档案")


@profile_app.command("show")
def show_profile(
    candidate_id: str = typer.Option(..., "--candidate-id", "-c"),
    profile_version: int | None = typer.Option(None, "--profile-version", min=1),
    json_output: bool = typer.Option(False, "--json", help="Emit CandidateProfile JSON."),
) -> None:
    """Display one candidate profile version, defaulting to the latest."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        try:
            profile = repository.get_candidate_profile(candidate_id, profile_version)
        except JobIntelError as exc:
            _abort(
                console,
                str(exc),
                code="PROFILE_NOT_FOUND",
                json_output=json_output,
            )
        if json_output:
            typer.echo(profile.model_dump_json(indent=2))
        else:
            _render_profile(console, profile, title="候选人档案")
    finally:
        database.close()


@radar_app.command("check")
def check_radar(
    baseline_run_id: str = typer.Argument(..., help="Baseline or latest radar run ID."),
    detail_top: int = typer.Option(
        0,
        "--detail-top",
        min=0,
        max=10,
        help="Conservatively refresh full JDs for at most ten top jobs.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the cooldown for controlled testing; source delays still apply.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit RadarCheck JSON."),
) -> None:
    """Refresh a saved BOSS search after its safe cooldown and report changes."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        discovery = JobDiscoveryService(
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
        service = JobRadarService(repository, discovery)
        try:
            if json_output:
                check = service.check(
                    baseline_run_id,
                    cooldown_hours=settings.radar_min_interval_hours,
                    detail_limit=detail_top,
                    detail_cache_hours=settings.discovery_detail_cache_hours,
                    force=force,
                )
            else:
                with console.status("[bold]正在执行低频职位雷达检查[/bold]…"):
                    check = service.check(
                        baseline_run_id,
                        cooldown_hours=settings.radar_min_interval_hours,
                        detail_limit=detail_top,
                        detail_cache_hours=settings.discovery_detail_cache_hours,
                        force=force,
                    )
        except (JobIntelError, RuntimeError, ValueError) as exc:
            _abort(console, str(exc), code="RADAR_CHECK_FAILED", json_output=json_output)
        if json_output:
            typer.echo(check.model_dump_json(indent=2))
        else:
            _render_radar(console, check)
    finally:
        database.close()


@radar_app.command("show")
def show_radar(
    run_id: str = typer.Argument(..., help="Persisted radar run ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit RadarCheck JSON."),
) -> None:
    """Display one persisted radar comparison without contacting BOSS."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        try:
            check = repository.get_radar_check(run_id)
        except JobIntelError as exc:
            _abort(console, str(exc), code="RADAR_NOT_FOUND", json_output=json_output)
        if json_output:
            typer.echo(check.model_dump_json(indent=2))
        else:
            _render_radar(console, check)
    finally:
        database.close()


@app.command()
def seed(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Migrate and atomically reload the JobIntel fixture database."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        stats = seed_database(database)
        if json_output:
            _emit_json(
                {
                    "database": str(settings.jobintel_db_path),
                    "rows": stats,
                }
            )
            return
        table = Table(title="Seeded JobIntel", show_header=True)
        table.add_column("Entity")
        table.add_column("Rows", justify="right")
        for name, count in stats.items():
            table.add_row(name, str(count))
        console.print(table)
        console.print(f"[green]Database ready at[/green] {settings.jobintel_db_path}")
    finally:
        database.close()


@app.command()
def analyze(
    candidate_id: str = typer.Option(..., "--candidate-id", "-c"),
    profile_version: int | None = typer.Option(None, "--profile-version", min=1),
    job_id: str | None = typer.Option(None, "--job-id", "-j"),
    job_version: int | None = typer.Option(None, "--job-version", min=1),
    jd_text: str | None = typer.Option(None, "--jd-text"),
    jd_file: Path | None = typer.Option(
        None,
        "--jd-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    provider: JobIntelProviderName | None = typer.Option(
        None, "--provider", "-p", help="Override LLM_PROVIDER for this run."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run parsing, guardrails, and scoring without persistence.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JobAnalysis JSON."),
) -> None:
    """Analyze one stored job or raw JD for a candidate profile."""
    console = _console()
    selected_sources = sum(value is not None for value in (job_id, jd_text, jd_file))
    if selected_sources != 1:
        _abort(
            console,
            "Provide exactly one of --job-id, --jd-text, or --jd-file.",
            code="INVALID_JOB_SOURCE",
            json_output=json_output,
        )
    if job_id is None and job_version is not None:
        _abort(
            console,
            "--job-version is only valid with --job-id.",
            code="INVALID_JOB_VERSION",
            json_output=json_output,
        )
    try:
        raw_text = jd_file.read_text(encoding="utf-8") if jd_file is not None else jd_text
    except (OSError, UnicodeError) as exc:
        _abort(
            console,
            f"Could not read JD file: {exc}",
            code="JD_FILE_READ_FAILED",
            json_output=json_output,
        )

    settings = load_jobintel_settings()
    if provider is not None:
        settings.llm_provider = provider
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        try:
            llm = build_jobintel_provider(settings)
            request = AnalysisRequest(
                candidate_id=candidate_id,
                profile_version=profile_version,
                job_id=job_id,
                job_version=job_version,
                jd_text=raw_text,
                jd_source_url=str(jd_file) if jd_file is not None else None,
            )
            agent = JobIntelAgent(llm, repository, settings)
            if json_output:
                result = asyncio.run(agent.analyze(request, dry_run=dry_run))
            else:
                target = job_id or (jd_file.name if jd_file is not None else "raw JD")
                with console.status(
                    f"[bold]Analyzing[/bold] {target!r} for {candidate_id!r} via {llm.name}…"
                ):
                    result = asyncio.run(agent.analyze(request, dry_run=dry_run))
        except JobIntelAgentError as exc:
            _abort(
                console,
                str(exc),
                code=exc.code.value,
                json_output=json_output,
            )
        except (JobIntelError, RuntimeError, ValueError) as exc:
            _abort(
                console,
                str(exc),
                code="ANALYZE_FAILED",
                json_output=json_output,
            )

        if json_output:
            typer.echo(result.analysis.model_dump_json(indent=2))
        else:
            job = (
                repository.get_job(result.analysis.job_id, result.analysis.job_version)
                if not dry_run or job_id is not None
                else None
            )
            _render_result(console, result, job=job, dry_run=dry_run)
    finally:
        database.close()


@app.command("show-analysis")
def show_analysis(
    analysis_id: str = typer.Argument(..., help="Persisted analysis ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JobAnalysis JSON."),
) -> None:
    """Load and display one complete persisted analysis aggregate."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        try:
            analysis = repository.get_analysis(analysis_id)
            job = repository.get_job(analysis.job_id, analysis.job_version)
        except JobIntelError as exc:
            _abort(
                console,
                str(exc),
                code="ANALYSIS_NOT_FOUND",
                json_output=json_output,
            )
        if json_output:
            typer.echo(analysis.model_dump_json(indent=2))
        else:
            _render_analysis(console, analysis, job)
    finally:
        database.close()


@app.command("list-analyses")
def list_analyses(
    candidate_id: str | None = typer.Option(None, "--candidate-id", "-c"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    json_output: bool = typer.Option(False, "--json", help="Emit JobAnalysis JSON list."),
) -> None:
    """List persisted deep analyses, newest first."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        analyses = repository.list_analyses(candidate_id=candidate_id, limit=limit)
        if json_output:
            _emit_json([item.model_dump(mode="json") for item in analyses])
        else:
            table = Table(title=f"历史深入分析 ({len(analyses)} 条)", expand=True)
            table.add_column("分析 ID")
            table.add_column("候选人")
            table.add_column("职位 ID")
            table.add_column("分数", justify="right")
            table.add_column("建议")
            table.add_column("时间")
            for item in analyses:
                table.add_row(
                    item.analysis_id,
                    f"{item.candidate_id}@{item.profile_version}",
                    f"{item.job_id}@{item.job_version}",
                    str(item.score),
                    _recommendation_label(item.recommendation),
                    item.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                )
            console.print(table)
            console.print("查看完整内容: jobintel show-analysis <分析 ID>")
    finally:
        database.close()


@outreach_app.command("generate")
def generate_outreach(
    analysis_id: str = typer.Option(..., "--analysis-id", help="已保存的深入分析 ID。"),
    tone: OutreachTone = typer.Option(OutreachTone.PROFESSIONAL, "--tone"),
    focus_requirement_ids: list[str] = typer.Option(
        [], "--focus-requirement-id", help="可重复指定优先写入的岗位要求 ID。"
    ),
    provider: JobIntelProviderName | None = typer.Option(None, "--provider", "-p"),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
) -> None:
    """从已验证分析和简历证据生成中文 HR 沟通草稿."""
    settings = load_jobintel_settings()
    if provider is not None:
        settings.llm_provider = provider
    console = _console()
    try:
        llm = build_jobintel_provider(settings)
    except (ImportError, RuntimeError, ValueError) as exc:
        _abort(console, str(exc), code="OUTREACH_PROVIDER_UNAVAILABLE", json_output=json_output)
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        service = OutreachService(
            llm,
            repository,
            max_repairs=settings.outreach_max_repairs,
        )
        try:
            if json_output:
                result = asyncio.run(
                    service.generate(
                        analysis_id=analysis_id,
                        tone=tone,
                        focus_requirement_ids=tuple(focus_requirement_ids),
                        run_id=run_id,
                    )
                )
            else:
                with console.status("[bold]正在生成并校验 HR 沟通草稿…[/bold]"):
                    result = asyncio.run(
                        service.generate(
                            analysis_id=analysis_id,
                            tone=tone,
                            focus_requirement_ids=tuple(focus_requirement_ids),
                            run_id=run_id,
                        )
                    )
        except OutreachGenerationError as exc:
            details = ", ".join(item.code.value for item in exc.violations)
            message = str(exc) + (f"; 最终校验问题: {details}" if details else "")
            _abort(console, message, code="OUTREACH_GENERATION_FAILED", json_output=json_output)
        except (JobIntelError, RuntimeError, ValueError) as exc:
            _abort(console, str(exc), code="OUTREACH_GENERATION_FAILED", json_output=json_output)
        if json_output:
            _emit_json(result.model_dump(mode="json"))
        else:
            _render_outreach(console, result.outreach)
            console.print(
                f"审核通过后运行: [bold]jobintel outreach approve "
                f"{result.outreach.outreach_id}[/bold]"
            )
    finally:
        database.close()


@outreach_app.command("show")
def show_outreach(
    outreach_id: str = typer.Argument(..., help="沟通草稿 ID。"),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看最新或指定版本的沟通草稿及审计事件."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        try:
            draft = repository.get_outreach(outreach_id, revision)
            events = repository.list_outreach_events(outreach_id)
        except JobIntelError as exc:
            _abort(console, str(exc), code="OUTREACH_NOT_FOUND", json_output=json_output)
        if json_output:
            _emit_json(
                {
                    "outreach": draft.model_dump(mode="json"),
                    "events": [item.model_dump(mode="json") for item in events],
                }
            )
        else:
            _render_outreach(console, draft)
            if events:
                console.print(
                    "操作记录: "
                    + " → ".join(f"{item.event_type.value}@v{item.revision}" for item in events)
                )
    finally:
        database.close()


@outreach_app.command("list")
def list_outreach_drafts(
    candidate_id: str | None = typer.Option(None, "--candidate-id", "-c"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出每份沟通草稿的最新版本."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        drafts = repository.list_outreach(candidate_id=candidate_id, limit=limit)
        if json_output:
            _emit_json([item.model_dump(mode="json") for item in drafts])
        else:
            table = Table(title=f"HR 沟通草稿 ({len(drafts)} 条)", expand=True)
            table.add_column("草稿 ID")
            table.add_column("版本")
            table.add_column("候选人")
            table.add_column("岗位")
            table.add_column("状态")
            table.add_column("更新时间")
            for item in drafts:
                table.add_row(
                    item.outreach_id,
                    str(item.revision),
                    f"{item.candidate_id}@{item.profile_version}",
                    f"{item.job_id}@{item.job_version}",
                    item.status.value,
                    item.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                )
            console.print(table)
    finally:
        database.close()


@outreach_app.command("revise")
def revise_outreach(
    outreach_id: str = typer.Argument(...),
    message: str | None = typer.Option(None, "--message"),
    message_file: Path | None = typer.Option(
        None,
        "--message-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """将人工修改后的完整文案保存为一个新的待审核版本."""
    console = _console()
    if (message is None) == (message_file is None):
        _abort(
            console,
            "请且仅请提供 --message 或 --message-file。",
            code="INVALID_OUTREACH_REVISION",
            json_output=json_output,
        )
    try:
        if message is not None:
            edited = message
        else:
            assert message_file is not None
            edited = message_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _abort(console, str(exc), code="OUTREACH_FILE_READ_FAILED", json_output=json_output)
    _perform_outreach_action(
        outreach_id,
        action="revise",
        revision=None,
        message=edited,
        json_output=json_output,
    )


def _perform_outreach_action(
    outreach_id: str,
    *,
    action: str,
    revision: int | None,
    json_output: bool,
    message: str | None = None,
) -> None:
    """Run one local review action with consistent persistence and output."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        service = OutreachService(None, repository)
        try:
            if action == "revise":
                assert message is not None
                draft = service.revise(outreach_id, message)
            else:
                operations = {
                    OutreachEventType.APPROVED.value: service.approve,
                    OutreachEventType.COPIED.value: service.record_copied,
                    OutreachEventType.OPENED.value: service.record_opened,
                    OutreachEventType.SENT_CONFIRMED.value: service.confirm_sent,
                    OutreachEventType.DISMISSED.value: service.dismiss,
                }
                draft = operations[action](outreach_id, revision=revision)
        except (JobIntelError, RuntimeError, ValueError) as exc:
            _abort(console, str(exc), code="OUTREACH_ACTION_FAILED", json_output=json_output)
        if json_output:
            _emit_json(draft.model_dump(mode="json"))
        else:
            _render_outreach(console, draft)
    finally:
        database.close()


@outreach_app.command("approve")
def approve_outreach(
    outreach_id: str = typer.Argument(...),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """人工确认草稿内容可以使用; 此命令不会发送消息."""
    _perform_outreach_action(
        outreach_id,
        action=OutreachEventType.APPROVED.value,
        revision=revision,
        json_output=json_output,
    )


@outreach_app.command("mark-copied")
def mark_outreach_copied(
    outreach_id: str = typer.Argument(...),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """记录用户已复制获批文案; 不会访问 BOSS."""
    _perform_outreach_action(
        outreach_id,
        action=OutreachEventType.COPIED.value,
        revision=revision,
        json_output=json_output,
    )


@outreach_app.command("mark-opened")
def mark_outreach_opened(
    outreach_id: str = typer.Argument(...),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """记录用户已自行打开岗位页面; 不会访问 BOSS."""
    _perform_outreach_action(
        outreach_id,
        action=OutreachEventType.OPENED.value,
        revision=revision,
        json_output=json_output,
    )


@outreach_app.command("mark-sent")
def mark_outreach_sent(
    outreach_id: str = typer.Argument(...),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """记录用户已在平台手动发送获批文案; 不会代为发送."""
    _perform_outreach_action(
        outreach_id,
        action=OutreachEventType.SENT_CONFIRMED.value,
        revision=revision,
        json_output=json_output,
    )


@outreach_app.command("dismiss")
def dismiss_outreach(
    outreach_id: str = typer.Argument(...),
    revision: int | None = typer.Option(None, "--revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """放弃最新草稿版本."""
    _perform_outreach_action(
        outreach_id,
        action=OutreachEventType.DISMISSED.value,
        revision=revision,
        json_output=json_output,
    )


@notify_app.command("discovery")
def notify_discovery_email(
    run_id: str = typer.Argument(..., help="已保存的职位搜索批次 ID。"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """将职位搜索批次发送到其候选人档案绑定的通知邮箱."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        try:
            run = repository.get_discovery_run(run_id)
            preference = repository.get_candidate_email_preference(run.preference.candidate_id)
            sender = build_email_sender(settings, recipient=preference.recipient_email)
            if json_output:
                receipt = DiscoveryEmailNotificationService(repository, sender).send_discovery(
                    run_id, limit=limit
                )
            else:
                with console.status("[bold]正在发送职位通知邮件…[/bold]"):
                    receipt = DiscoveryEmailNotificationService(repository, sender).send_discovery(
                        run_id, limit=limit
                    )
        except (EmailNotificationError, JobIntelError, RuntimeError, ValueError) as exc:
            _abort(console, str(exc), code="EMAIL_NOTIFICATION_FAILED", json_output=json_output)
        if json_output:
            _emit_json(receipt.model_dump(mode="json"))
        else:
            console.print(
                f"[green]已发送 {receipt.job_count} 个职位到 {receipt.recipient_masked}[/green]"
            )
    finally:
        database.close()


@app.command("analyze-discovery")
def analyze_discovery(
    run_id: str = typer.Argument(..., help="Persisted discovery run ID."),
    top: int = typer.Option(3, "--top", min=1, max=10),
    provider: JobIntelProviderName | None = typer.Option(None, "--provider", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not persist analyses."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Deep-analyze saved discovery results without contacting job sources again."""
    settings = load_jobintel_settings()
    if provider is not None:
        settings.llm_provider = provider
    console = _console()
    try:
        llm = build_jobintel_provider(settings)
    except (ImportError, RuntimeError, ValueError) as exc:
        _abort(
            console,
            str(exc),
            code="ANALYSIS_PROVIDER_UNAVAILABLE",
            json_output=json_output,
        )
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        try:
            run = repository.get_discovery_run(run_id)
            if json_output:
                analyses = _analyze_discovery_hits(
                    run,
                    analyze_top=top,
                    repository=repository,
                    settings=settings,
                    dry_run=dry_run,
                    llm=llm,
                )
            else:
                with console.status(
                    f"[bold]正在深入分析[/bold] 已保存批次 {run_id} 的前 {top} 个职位…"
                ):
                    analyses = _analyze_discovery_hits(
                        run,
                        analyze_top=top,
                        repository=repository,
                        settings=settings,
                        dry_run=dry_run,
                        llm=llm,
                    )
        except (JobIntelError, RuntimeError, ValueError) as exc:
            _abort(
                console,
                str(exc),
                code="ANALYZE_DISCOVERY_FAILED",
                json_output=json_output,
            )
    finally:
        database.close()

    if json_output:
        _emit_json({"run_id": run_id, "analyses": analyses, "dry_run": dry_run})
    else:
        _render_analysis_batch(console, run, analyses, dry_run=dry_run)


@app.command("serve-mcp")
def serve_mcp() -> None:
    """Run the six-tool JobIntel FastMCP server over stdio."""
    from jobintel.mcp_server.server import main

    main()


@app.command("web")
def serve_web(
    host: str = typer.Option("127.0.0.1", "--host", help="Listen address."),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Acknowledge that non-loopback binding has no built-in authentication.",
    ),
) -> None:
    """Run the local JobIntel browser workspace."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        _abort(
            _console(),
            "Web 工作台没有内置登录保护。请使用 127.0.0.1 + SSH 端口转发, "
            "或明确传入 --allow-remote。",
            code="REMOTE_WEB_BIND_REQUIRES_ACKNOWLEDGEMENT",
            json_output=False,
        )
    import uvicorn

    from jobintel.web.app import create_app

    _console().print(f"[green]JobIntel 工作台:[/green] http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command("setup-browser")
def setup_browser(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Launch the isolated local Chrome profile used for BOSS discovery."""
    settings = load_jobintel_settings()
    result = setup_chrome(settings.discovery_cdp_port)
    if json_output:
        _emit_json(result)
    else:
        color = "green" if result["ok"] else "red"
        _console().print(f"[{color}]{result['message']}[/{color}]")
        _console().print(f"Profile: {result['profile']} · CDP port: {result['port']}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@app.command("source-doctor")
def source_doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Check local prerequisites without contacting job platforms."""
    settings = load_jobintel_settings()
    browser_ready = cdp_reachable(settings.discovery_cdp_port)
    sources = [
        {
            "source": JobSource.BOSS.value,
            "ready": browser_ready,
            "mode": "local_chrome",
            "message": (
                "browser bridge reachable; login is checked during search"
                if browser_ready
                else "run jobintel setup-browser"
            ),
        }
    ]
    payload = {"cdp_port": settings.discovery_cdp_port, "sources": sources}
    if json_output:
        _emit_json(payload)
        return
    table = Table(title="Job source prerequisites")
    table.add_column("Source")
    table.add_column("Mode")
    table.add_column("Local readiness")
    table.add_column("Next step")
    for item in sources:
        table.add_row(
            str(item["source"]),
            str(item["mode"]),
            "ready" if item["ready"] else "not ready",
            str(item["message"]),
        )
    _console().print(table)


@app.command("show-discovery")
def show_discovery(
    run_id: str = typer.Argument(..., help="Persisted discovery run ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit DiscoveryRun JSON."),
) -> None:
    """Load one persisted discovery result with its original source links."""
    settings = load_jobintel_settings()
    console = _console()
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        repository = SQLiteJobRepository(database)
        try:
            run = repository.get_discovery_run(run_id)
        except JobIntelError as exc:
            _abort(
                console,
                str(exc),
                code="DISCOVERY_NOT_FOUND",
                json_output=json_output,
            )
        if json_output:
            typer.echo(run.model_dump_json(indent=2))
        else:
            _render_discovery(console, run, analyses=[], dry_run=False)
    finally:
        database.close()


@app.command()
def discover(
    candidate_id: str = typer.Option(..., "--candidate-id", "-c"),
    query: str = typer.Option(..., "--query", "-q", help="Target role or search phrase."),
    profile_version: int | None = typer.Option(None, "--profile-version", min=1),
    city: str = typer.Option("", "--city"),
    salary_min: int | None = typer.Option(
        None, "--salary-min", min=0, help="Monthly salary lower bound in K."
    ),
    salary_max: int | None = typer.Option(
        None, "--salary-max", min=0, help="Monthly salary upper bound in K."
    ),
    daily_salary_min: int | None = typer.Option(
        None, "--daily-salary-min", min=0, help="Daily salary lower bound in yuan."
    ),
    daily_salary_max: int | None = typer.Option(
        None, "--daily-salary-max", min=0, help="Daily salary upper bound in yuan."
    ),
    employment_type: list[EmploymentType] | None = typer.Option(
        None, "--employment-type", help="Repeat to accept multiple employment types."
    ),
    company_size: list[CompanySize] | None = typer.Option(
        None, "--company-size", help="Repeat to accept multiple company-size bands."
    ),
    education: list[str] | None = typer.Option(
        None, "--education", help="Repeat to accept multiple education labels."
    ),
    experience: list[str] | None = typer.Option(
        None, "--experience", help="Repeat to accept multiple experience labels."
    ),
    exclude: list[str] | None = typer.Option(None, "--exclude"),
    exclude_outsourcing: bool = typer.Option(
        False, "--exclude-outsourcing", help="Exclude outsourcing and onsite-vendor roles."
    ),
    exclude_training: bool = typer.Option(
        False, "--exclude-training", help="Exclude training and paid-training roles."
    ),
    exclude_agency: bool = typer.Option(
        False, "--exclude-agency", help="Exclude recruiter and staffing-agency listings."
    ),
    strict_salary: bool = typer.Option(
        False, "--strict-salary", help="Exclude jobs whose salary is undisclosed."
    ),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
    analyze_top: int = typer.Option(
        0,
        "--analyze-top",
        min=0,
        max=10,
        help="Fetch full JDs and analyze the highest-ranked jobs (maximum 10).",
    ),
    detail_top: int = typer.Option(
        0,
        "--detail-top",
        min=0,
        max=10,
        help="Slowly fetch and cache full JDs for the highest-ranked jobs.",
    ),
    provider: JobIntelProviderName | None = typer.Option(None, "--provider", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not persist discovery or analyses."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Batch-discover real jobs, rank them, and optionally analyze Top-K."""
    console = _console()
    exclusions = list(exclude or ())
    if exclude_outsourcing:
        exclusions.extend(_EXCLUSION_PRESETS["outsourcing"])
    if exclude_training:
        exclusions.extend(_EXCLUSION_PRESETS["training"])
    if exclude_agency:
        exclusions.extend(_EXCLUSION_PRESETS["agency"])
    try:
        preference = JobSearchPreference(
            candidate_id=candidate_id,
            profile_version=profile_version,
            query=query,
            city=city,
            salary_min_k=salary_min,
            salary_max_k=salary_max,
            daily_salary_min_yuan=daily_salary_min,
            daily_salary_max_yuan=daily_salary_max,
            employment_types=tuple(employment_type or ()),
            company_sizes=tuple(company_size or ()),
            education_requirements=tuple(education or ()),
            experience_requirements=tuple(experience or ()),
            include_undisclosed_salary=not strict_salary,
            exclusions=tuple(dict.fromkeys(exclusions)),
            sources=(JobSource.BOSS,),
            limit=limit,
        )
    except ValueError as exc:
        _abort(console, str(exc), code="INVALID_DISCOVERY_REQUEST", json_output=json_output)

    settings = load_jobintel_settings()
    if provider is not None:
        settings.llm_provider = provider
    analysis_llm: LLMProvider | None = None
    if analyze_top:
        try:
            analysis_llm = build_jobintel_provider(settings)
        except (ImportError, RuntimeError, ValueError) as exc:
            _abort(
                console,
                str(exc),
                code="ANALYSIS_PROVIDER_UNAVAILABLE",
                json_output=json_output,
            )
    database = JobIntelDatabase.connect(settings.jobintel_db_path)
    try:
        _ensure_seeded(database)
        repository = SQLiteJobRepository(database)
        service = JobDiscoveryService(
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
        effective_detail_top = max(detail_top, analyze_top)
        if json_output:
            run = service.discover(
                preference,
                persist=not dry_run,
                detail_limit=effective_detail_top,
                detail_cache_hours=settings.discovery_detail_cache_hours,
            )
        else:
            with console.status(f"[bold]Searching[/bold] {query!r} across live sources…"):
                run = service.discover(
                    preference,
                    persist=not dry_run,
                    detail_limit=effective_detail_top,
                    detail_cache_hours=settings.discovery_detail_cache_hours,
                )
        analyses = _analyze_discovery_hits(
            run,
            analyze_top=analyze_top,
            repository=repository,
            settings=settings,
            dry_run=dry_run,
            llm=analysis_llm,
        )
    except (JobIntelError, RuntimeError, ValueError) as exc:
        _abort(console, str(exc), code="DISCOVERY_FAILED", json_output=json_output)
    finally:
        database.close()

    if json_output:
        _emit_json(
            {
                "discovery": run.model_dump(mode="json"),
                "analyses": analyses,
            }
        )
    else:
        _render_discovery(console, run, analyses=analyses, dry_run=dry_run)


def _recommendation_label(value: Recommendation) -> str:
    """Return the stable Chinese presentation label for a recommendation."""
    return {
        Recommendation.STRONG_APPLY: "强烈建议申请",
        Recommendation.APPLY: "建议申请",
        Recommendation.LOW_PRIORITY: "低优先级",
        Recommendation.SKIP: "暂不建议申请",
    }[value]


def _render_radar(console: Console, check: RadarCheck) -> None:
    """Render actionable radar changes while retaining honest closed semantics."""
    labels = {
        RadarEventStatus.NEW: "新增",
        RadarEventStatus.CHANGED: "信息变化",
        RadarEventStatus.UNCHANGED: "未变化",
        RadarEventStatus.CLOSED: "疑似下线/本次未见",
    }
    counts = {
        status: sum(event.status is status for event in check.events) for status in RadarEventStatus
    }
    summary = " · ".join(f"{labels[status]} {counts[status]}" for status in RadarEventStatus)
    console.print(
        Panel(
            summary,
            title=f"职位雷达 {check.run_id}",
            subtitle=f"基线 {check.baseline_run_id}",
            border_style="cyan",
        )
    )
    table = Table(title="需要关注的变化", expand=True)
    table.add_column("状态")
    table.add_column("职位")
    table.add_column("公司")
    table.add_column("地点 / 薪资")
    table.add_column("BOSS")
    actionable = [event for event in check.events if event.status is not RadarEventStatus.UNCHANGED]
    for event in actionable:
        link = event.job.source_links[0]
        table.add_row(
            labels[event.status],
            event.job.title,
            event.job.company_name,
            " / ".join(value for value in (event.job.location, event.job.salary_text) if value),
            f"[link={link.url}]打开[/link]",
        )
    if actionable:
        console.print(table)
    else:
        console.print("本次没有需要关注的变化。")
    console.print(f"下次检查以本次结果为基线: jobintel radar check {check.run_id}")


def _render_analysis_batch(
    console: Console,
    run: DiscoveryRun,
    analyses: list[dict[str, object]],
    *,
    dry_run: bool,
) -> None:
    """Render identifiers and outcomes for analyses started from discovery hits."""
    jobs_by_id = {hit.job.discovery_job_id: hit.job for hit in run.hits}
    table = Table(title=f"深入分析结果 ({len(analyses)} 个)", expand=True)
    table.add_column("职位")
    table.add_column("分数", justify="right")
    table.add_column("建议")
    table.add_column("分析 ID / 错误")
    succeeded = 0
    for item in analyses:
        discovery_job_id = str(item["discovery_job_id"])
        job = jobs_by_id.get(discovery_job_id)
        job_label = f"{job.title} · {job.company_name}" if job is not None else discovery_job_id
        payload = item.get("analysis")
        if isinstance(payload, dict):
            analysis = JobAnalysis.model_validate(payload)
            succeeded += 1
            table.add_row(
                job_label,
                str(analysis.score),
                _recommendation_label(analysis.recommendation),
                analysis.analysis_id,
            )
        else:
            error = item.get("error")
            message = (
                str(error.get("message", "分析失败")) if isinstance(error, dict) else "分析失败"
            )
            table.add_row(job_label, "—", "失败", message)
    console.print(table)
    persistence = "试运行, 未保存" if dry_run else "已保存"
    console.print(f"完成 {succeeded}/{len(analyses)}, {persistence}。")
    if succeeded and not dry_run:
        console.print("完整内容: jobintel show-analysis <分析 ID>")


def _render_discovery(
    console: Console,
    run: DiscoveryRun,
    *,
    analyses: list[dict[str, object]],
    dry_run: bool,
) -> None:
    """Render live source health and ranked, directly clickable jobs."""
    source_table = Table(title="Source status")
    source_table.add_column("Source")
    source_table.add_column("Status")
    source_table.add_column("Found", justify="right")
    source_table.add_column("Time", justify="right")
    source_table.add_column("Message")
    for attempt in run.source_attempts:
        source_table.add_row(
            attempt.source.value,
            attempt.status.value,
            str(attempt.discovered_count),
            f"{attempt.elapsed_ms} ms",
            attempt.message or "",
        )
    console.print(source_table)

    if run.detail_attempts:
        detail_table = Table(title="Full JD enrichment")
        detail_table.add_column("Job")
        detail_table.add_column("Status")
        detail_table.add_column("Time", justify="right")
        detail_table.add_column("Message")
        jobs_by_id = {hit.job.discovery_job_id: hit.job for hit in run.hits}
        for detail_attempt in run.detail_attempts:
            job = jobs_by_id[detail_attempt.discovery_job_id]
            detail_table.add_row(
                f"{job.title} · {job.company_name}",
                detail_attempt.status.value,
                f"{detail_attempt.elapsed_ms} ms",
                detail_attempt.message or "",
            )
        console.print(detail_table)

    jobs = Table(title=f"Ranked jobs · {len(run.hits)} results", expand=True)
    jobs.add_column("#", justify="right")
    jobs.add_column("Score", justify="right")
    jobs.add_column("Job")
    jobs.add_column("Company")
    jobs.add_column("Location / Salary")
    jobs.add_column("Source links")
    for index, hit in enumerate(run.hits, start=1):
        links = "\n".join(
            f"[link={link.url}]{link.source.value}[/link]" for link in hit.job.source_links
        )
        jobs.add_row(
            str(index),
            str(hit.rank_score),
            hit.job.title,
            hit.job.company_name,
            " / ".join(value for value in (hit.job.location, hit.job.salary_text) if value),
            links,
        )
    console.print(jobs)
    persistence = "dry run — not saved" if dry_run else f"saved as {run.run_id}"
    console.print(
        f"Discovered {run.total_discovered}; removed {run.duplicates_removed} duplicates; "
        f"filtered {run.filtered_out}; {persistence}."
    )
    if analyses:
        _render_analysis_batch(console, run, analyses, dry_run=dry_run)


def _render_result(
    console: Console,
    result: JobIntelAgentResult,
    *,
    job: JobPosting | None,
    dry_run: bool,
) -> None:
    """Render analysis plus content-free run telemetry."""
    _render_analysis(console, result.analysis, job)
    telemetry = result.telemetry
    meta = Table.grid(padding=(0, 2))
    meta.add_row("模型服务", telemetry.provider)
    meta.add_row("迭代 / 修复次数", f"{telemetry.iterations} / {telemetry.repairs}")
    meta.add_row("工具调用", ", ".join(telemetry.tool_calls) or "—")
    meta.add_row("Token", f"输入 {telemetry.input_tokens} / 输出 {telemetry.output_tokens}")
    meta.add_row("提示词版本", telemetry.prompt_version)
    meta.add_row("持久化", "试运行 — 未保存" if dry_run else "已保存")
    console.print(Panel(meta, title="运行信息", border_style="dim"))


def _render_analysis(console: Console, analysis: JobAnalysis, job: JobPosting | None) -> None:
    """Render one finalized analysis for human review."""
    status_labels = {
        MatchStatus.MATCHED: "匹配",
        MatchStatus.PARTIAL: "部分匹配",
        MatchStatus.MISSING: "缺失证据",
    }
    title = job.title if job is not None else f"职位 {analysis.job_id}"
    summary = (
        f"[bold]{title}[/bold]\n"
        f"候选人: {analysis.candidate_id}@{analysis.profile_version}\n"
        f"匹配分: [bold]{analysis.score}/100[/bold]\n"
        f"申请建议: [bold]{_recommendation_label(analysis.recommendation)}[/bold]"
    )
    console.print(Panel(summary, title=f"分析 {analysis.analysis_id}", border_style="cyan"))

    requirement_text = (
        {item.requirement_id: item.text for item in job.requirements} if job is not None else {}
    )
    table = Table(title="岗位要求逐项评估", expand=True)
    table.add_column("岗位要求")
    table.add_column("状态")
    table.add_column("候选人证据")
    table.add_column("判断理由")
    for match in analysis.requirement_matches:
        table.add_row(
            requirement_text.get(match.requirement_id, match.requirement_id),
            status_labels[match.status],
            ", ".join(match.evidence_ids) or "—",
            match.reason,
        )
    console.print(table)

    if analysis.strengths:
        console.print(
            Panel(
                "\n".join(f"• {item.text}" for item in analysis.strengths),
                title="有证据支持的优势",
                border_style="green",
            )
        )
    if analysis.missing_skills:
        console.print(
            Panel(
                "\n".join(f"• {item.skill}" for item in analysis.missing_skills),
                title="缺失证据的能力",
                border_style="yellow",
            )
        )
    if analysis.resume_suggestions:
        console.print(
            Panel(
                "\n".join(f"• {item.text}" for item in analysis.resume_suggestions),
                title="简历修改建议",
            )
        )
    if analysis.interview_topics:
        console.print(
            Panel(
                "\n".join(f"• {item.text}" for item in analysis.interview_topics),
                title="面试准备重点",
            )
        )
    console.print(Panel(analysis.next_action, title="建议的下一步行动", border_style="magenta"))


if __name__ == "__main__":
    app()
