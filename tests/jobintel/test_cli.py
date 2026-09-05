from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from tests.conftest import FakeProvider
from tests.jobintel.outreach_fixtures import build_outreach_scope, valid_outreach_message
from typer.testing import CliRunner

from jobintel import cli
from jobintel.discovery.models import CompanySize, JobSource, RawJobListing
from jobintel.models import (
    GroundedClaim,
    InterviewTopic,
    JobAnalysis,
    JobAnalysisDraft,
    MatchStatus,
    RequirementCategory,
    RequirementMatchDraft,
    ResumeSuggestion,
    stable_requirement_id,
)
from jobintel.notifications.models import CandidateEmailPreference
from jobintel.outreach.service import OUTREACH_SUBMIT_TOOL
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database
from jobintel.providers.base import ToolCall, TurnResult, Usage
from jobintel.services.evidence_search import EvidenceSearchService
from jobintel.services.jd_parser import PARSER_SUBMIT_TOOL, raw_job_id

runner = CliRunner()
_USAGE = Usage(input_tokens=10, output_tokens=5)
_RAW_JD = """Orbit Labs Platform Engineer
Must have production Python experience.
Kubernetes deployment experience is preferred.
"""


class _DiscoveryConnector:
    source = JobSource.BOSS

    def search(self, _preference: object, *, limit: int) -> tuple[RawJobListing, ...]:
        return (
            RawJobListing(
                source=JobSource.BOSS,
                external_id="boss-live-1",
                title="Python 后端工程师",
                company_name="真实科技",
                location="上海",
                salary_text="25-40K",
                description="Python FastAPI PostgreSQL",
                experience="3-5年",
                education="本科",
                company_size=CompanySize.SMALL,
                url="https://www.zhipin.com/job_detail/boss-live-1.html",
                published_text="今天",
            ),
        )[:limit]


class _EmailSender:
    from_address = "jobs@example.com"
    recipient = "owner@example.net"

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def _seeded_repository(
    path: Path,
) -> tuple[JobIntelDatabase, SQLiteJobRepository]:
    database = JobIntelDatabase.connect(path)
    seed_database(database)
    return database, SQLiteJobRepository(database)


def _job_script(repository: SQLiteJobRepository, job_id: str) -> list[TurnResult]:
    job = repository.get_job(job_id)
    profile = repository.get_candidate_profile("C001", 2)
    scope_calls = [
        ToolCall(
            id=f"{job_id}-job",
            name="get_job",
            arguments={"job_id": job.job_id, "job_version": job.job_version},
        ),
        ToolCall(
            id=f"{job_id}-parse",
            name="parse_job_requirements",
            arguments={"job_id": job.job_id, "job_version": job.job_version},
        ),
        ToolCall(
            id=f"{job_id}-profile",
            name="get_candidate_profile",
            arguments={"candidate_id": profile.candidate_id, "profile_version": 2},
        ),
    ]
    if job.company_id is not None:
        scope_calls.append(
            ToolCall(
                id=f"{job_id}-company",
                name="get_company",
                arguments={"company_id": job.company_id},
            )
        )

    search = EvidenceSearchService()
    search_calls = []
    matches = []
    for requirement in job.requirements:
        query = requirement.normalized_skill or requirement.text
        output = search.search(
            job=job,
            requirement=requirement,
            profile=profile,
            query=query,
        )
        evidence_ids = (output.hits[0].evidence.evidence_id,) if output.hits else ()
        search_calls.append(
            ToolCall(
                id=f"{job_id}-search-{requirement.source_order}",
                name="search_candidate_evidence",
                arguments={
                    "job_id": job.job_id,
                    "job_version": job.job_version,
                    "requirement_id": requirement.requirement_id,
                    "candidate_id": profile.candidate_id,
                    "profile_version": profile.profile_version,
                    "query": query,
                },
            )
        )
        matches.append(
            RequirementMatchDraft(
                requirement_id=requirement.requirement_id,
                status=MatchStatus.MATCHED if evidence_ids else MatchStatus.MISSING,
                evidence_ids=evidence_ids,
                confidence=0.9 if evidence_ids else 0.8,
                reason=(
                    "Scoped evidence supports this requirement."
                    if evidence_ids
                    else "No supporting evidence was returned."
                ),
            )
        )
    draft = JobAnalysisDraft(
        job_id=job.job_id,
        job_version=job.job_version,
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        requirement_matches=tuple(matches),
        strengths=tuple(
            GroundedClaim(
                claim_id="primary-strength",
                text="The profile contains direct evidence for a job requirement.",
                requirement_ids=(match.requirement_id,),
                evidence_ids=match.evidence_ids,
            )
            for match in matches
            if match.status is MatchStatus.MATCHED
        )[:1],
        resume_suggestions=(
            ResumeSuggestion(
                requirement_id=job.requirements[0].requirement_id,
                text="Quantify the most relevant experience.",
            ),
        ),
        interview_topics=(
            InterviewTopic(
                requirement_id=job.requirements[-1].requirement_id,
                text="Prepare a concrete example for this requirement.",
            ),
        ),
        next_action="Use the program recommendation to prioritize this application.",
    )
    return [
        TurnResult(tool_calls=scope_calls, usage=_USAGE),
        TurnResult(tool_calls=search_calls, usage=_USAGE),
        TurnResult(
            tool_calls=[
                ToolCall(
                    id=f"{job_id}-save",
                    name="save_application_analysis",
                    arguments=draft.model_dump(mode="json"),
                )
            ],
            usage=_USAGE,
        ),
    ]


@pytest.mark.parametrize("job_id", ["J001", "J002", "J003"])
def test_analyze_json_completes_three_seed_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
) -> None:
    db_path = tmp_path / f"{job_id}.db"
    database, repository = _seeded_repository(db_path)
    turns = _job_script(repository, job_id)
    database.close()
    provider = FakeProvider(turns)
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(cli, "build_jobintel_provider", lambda _settings: provider)

    result = runner.invoke(
        cli.app,
        ["analyze", "--candidate-id", "C001", "--job-id", job_id, "--json"],
    )

    assert result.exit_code == 0, result.stdout
    analysis = JobAnalysis.model_validate(json.loads(result.stdout))
    assert analysis.job_id == job_id
    assert len(analysis.requirement_matches) == 4
    assert provider.calls == 3
    database = JobIntelDatabase.connect(db_path)
    try:
        assert SQLiteJobRepository(database).get_analysis(analysis.analysis_id) == analysis
    finally:
        database.close()


def test_analyze_human_output_and_show_analysis_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobintel.db"
    database, repository = _seeded_repository(db_path)
    turns = _job_script(repository, "J001")
    database.close()
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(cli, "build_jobintel_provider", lambda _settings: FakeProvider(turns))

    analyzed = runner.invoke(cli.app, ["analyze", "--candidate-id", "C001", "--job-id", "J001"])

    assert analyzed.exit_code == 0, analyzed.stdout
    assert "岗位要求逐项评估" in analyzed.stdout
    assert "有证据支持的优势" in analyzed.stdout
    assert "简历修改建议" in analyzed.stdout
    assert "面试准备重点" in analyzed.stdout
    assert "运行信息" in analyzed.stdout
    assert "parse_job_requirements" in analyzed.stdout

    database = JobIntelDatabase.connect(db_path)
    try:
        analysis_id = database.connection.execute(
            "SELECT analysis_id FROM application_analyses"
        ).fetchone()[0]
    finally:
        database.close()
    shown = runner.invoke(cli.app, ["show-analysis", analysis_id, "--json"])
    assert shown.exit_code == 0
    assert JobAnalysis.model_validate(json.loads(shown.stdout)).analysis_id == analysis_id

    listed = runner.invoke(cli.app, ["list-analyses", "--candidate-id", "C001", "--json"])
    assert listed.exit_code == 0
    assert [item["analysis_id"] for item in json.loads(listed.stdout)] == [analysis_id]


def test_dry_run_returns_final_analysis_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dry.db"
    database, repository = _seeded_repository(db_path)
    turns = _job_script(repository, "J002")
    database.close()
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(cli, "build_jobintel_provider", lambda _settings: FakeProvider(turns))

    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--candidate-id",
            "C001",
            "--job-id",
            "J002",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    JobAnalysis.model_validate(json.loads(result.stdout))
    database = JobIntelDatabase.connect(db_path)
    try:
        count = database.connection.execute("SELECT COUNT(*) FROM application_analyses").fetchone()[
            0
        ]
        assert count == 0
    finally:
        database.close()


def _raw_script() -> list[TurnResult]:
    job_id = raw_job_id(_RAW_JD)
    specs = (
        ("Production Python experience", "Python", "ev-python-api", "must"),
        (
            "Kubernetes deployment experience",
            "Kubernetes",
            "ev-atlas-platform",
            "preferred",
        ),
    )
    requirement_ids = [
        stable_requirement_id(
            job_id=job_id,
            job_version=1,
            text=text,
            category=RequirementCategory.SKILL,
        )
        for text, _query, _evidence, _importance in specs
    ]
    parser = TurnResult(
        tool_calls=[
            ToolCall(
                id="raw-parser",
                name=PARSER_SUBMIT_TOOL,
                arguments={
                    "company_name": "Orbit Labs",
                    "title": "Platform Engineer",
                    "requirements": [
                        {
                            "text": text,
                            "category": "skill",
                            "importance": importance,
                            "normalized_skill": query,
                        }
                        for text, query, _evidence, importance in specs
                    ],
                },
            )
        ],
        usage=_USAGE,
    )
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
            for index, (requirement_id, (_text, query, _evidence, _importance)) in enumerate(
                zip(requirement_ids, specs, strict=True)
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
                evidence_ids=(evidence,),
                confidence=0.9,
                reason="Scoped evidence supports the raw JD requirement.",
            )
            for requirement_id, (_text, _query, evidence, _importance) in zip(
                requirement_ids, specs, strict=True
            )
        ),
        next_action="Apply.",
    )
    terminal = TurnResult(
        tool_calls=[
            ToolCall(
                id="raw-save",
                name="save_application_analysis",
                arguments=draft.model_dump(mode="json"),
            )
        ],
        usage=_USAGE,
    )
    return [parser, scope, searches, terminal]


def test_analyze_accepts_raw_jd_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "raw.db"
    jd_path = tmp_path / "role.txt"
    jd_path.write_text(_RAW_JD, encoding="utf-8")
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli, "build_jobintel_provider", lambda _settings: FakeProvider(_raw_script())
    )

    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--candidate-id",
            "C001",
            "--jd-file",
            str(jd_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    analysis = JobAnalysis.model_validate(json.loads(result.stdout))
    assert analysis.job_id == raw_job_id(_RAW_JD)
    database = JobIntelDatabase.connect(db_path)
    try:
        job = SQLiteJobRepository(database).get_job(analysis.job_id, 1)
        assert job.source_url == str(jd_path.resolve())
    finally:
        database.close()


def test_analyze_accepts_raw_jd_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "raw-text.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli, "build_jobintel_provider", lambda _settings: FakeProvider(_raw_script())
    )

    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--candidate-id",
            "C001",
            "--jd-text",
            _RAW_JD,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    analysis = JobAnalysis.model_validate(json.loads(result.stdout))
    assert analysis.job_id == raw_job_id(_RAW_JD)


def test_seed_rich_and_json_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "seed.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))

    rich_result = runner.invoke(cli.app, ["seed"])
    json_result = runner.invoke(cli.app, ["seed", "--json"])

    assert rich_result.exit_code == 0
    assert "Seeded JobIntel" in rich_result.stdout
    assert json.loads(json_result.stdout)["rows"]["jobs"] == 3


def test_invalid_source_fails_before_database_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    def fail_if_opened(_path: object) -> JobIntelDatabase:
        nonlocal opened
        opened = True
        raise AssertionError("database should not open")

    monkeypatch.setattr(cli.JobIntelDatabase, "connect", fail_if_opened)
    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "--candidate-id",
            "C001",
            "--job-id",
            "J001",
            "--jd-text",
            "raw",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_JOB_SOURCE"
    assert opened is False


def test_missing_analysis_and_provider_failure_close_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "close.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    captured: list[JobIntelDatabase] = []
    original_connect = JobIntelDatabase.connect

    def tracking_connect(path: Path | str = ":memory:") -> JobIntelDatabase:
        database = original_connect(path)
        captured.append(database)
        return database

    monkeypatch.setattr(cli.JobIntelDatabase, "connect", tracking_connect)
    missing = runner.invoke(cli.app, ["show-analysis", "missing", "--json"])
    assert missing.exit_code == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[-1].connection.execute("SELECT 1")

    def missing_key(_settings: object) -> object:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(cli, "build_jobintel_provider", missing_key)
    failed = runner.invoke(
        cli.app,
        ["analyze", "--candidate-id", "C001", "--job-id", "J001", "--json"],
    )
    assert failed.exit_code == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[-1].connection.execute("SELECT 1")


def test_serve_mcp_delegates_to_jobintel_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobintel.mcp_server.server as server_module

    called = False

    def fake_main() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(server_module, "main", fake_main)

    result = runner.invoke(cli.app, ["serve-mcp"])

    assert result.exit_code == 0
    assert called is True


def test_web_command_defaults_local_and_rejects_unacknowledged_remote_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    calls: list[tuple[str, int]] = []

    def fake_run(_app: object, *, host: str, port: int, log_level: str) -> None:
        assert log_level == "info"
        calls.append((host, port))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    local = runner.invoke(cli.app, ["web", "--port", "8123"])
    remote = runner.invoke(cli.app, ["web", "--host", "0.0.0.0"])

    assert local.exit_code == 0, local.stdout
    assert calls == [("127.0.0.1", 8123)]
    assert remote.exit_code == 1
    assert "没有内置登录保护" in remote.stdout


def test_discover_returns_live_ranked_jobs_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "discover.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _DiscoveryConnector()},
    )

    result = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python 后端",
            "--city",
            "上海",
            "--company-size",
            "small",
            "--limit",
            "10",
            "--detail-top",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["discovery"]["total_discovered"] == 1
    assert payload["discovery"]["hits"][0]["job"]["company_name"] == "真实科技"
    assert payload["discovery"]["preference"]["company_sizes"] == ["small"]
    assert payload["discovery"]["source_attempts"][0]["status"] == "success"
    assert payload["discovery"]["detail_attempts"][0]["status"] == "skipped"
    assert payload["analyses"] == []
    database = JobIntelDatabase.connect(db_path)
    try:
        repository = SQLiteJobRepository(database)
        run = repository.get_discovery_run(payload["discovery"]["run_id"])
        assert run.hits[0].job.title == "Python 后端工程师"
    finally:
        database.close()


def test_analyze_discovery_reuses_saved_run_without_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "analyze-discovery.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _DiscoveryConnector()},
    )
    discovered = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python",
            "--json",
        ],
    )
    assert discovered.exit_code == 0
    run_id = json.loads(discovered.stdout)["discovery"]["run_id"]

    monkeypatch.setattr(cli, "build_jobintel_provider", lambda _settings: object())
    monkeypatch.setattr(
        cli,
        "_analyze_discovery_hits",
        lambda run, **_kwargs: [
            {"discovery_job_id": run.hits[0].job.discovery_job_id, "error": {"message": "x"}}
        ],
    )
    monkeypatch.setattr(
        cli,
        "build_connectors",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not access BOSS")),
    )

    analyzed = runner.invoke(cli.app, ["analyze-discovery", run_id, "--top", "1", "--json"])

    assert analyzed.exit_code == 0, analyzed.stdout
    assert json.loads(analyzed.stdout)["run_id"] == run_id


def test_radar_check_reuses_saved_preference_and_persists_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "radar.db"
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _DiscoveryConnector()},
    )
    discovered = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python",
            "--limit",
            "10",
            "--json",
        ],
    )
    assert discovered.exit_code == 0
    run_id = json.loads(discovered.stdout)["discovery"]["run_id"]

    checked = runner.invoke(cli.app, ["radar", "check", run_id, "--force", "--json"])

    assert checked.exit_code == 0, checked.stdout
    payload = json.loads(checked.stdout)
    assert payload["baseline_run_id"] == run_id
    assert payload["events"][0]["status"] == "unchanged"
    shown = runner.invoke(cli.app, ["radar", "show", payload["run_id"], "--json"])
    assert shown.exit_code == 0


def test_discover_rejects_invalid_salary_range_before_database_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    def fail_if_opened(_path: object) -> JobIntelDatabase:
        nonlocal opened
        opened = True
        raise AssertionError("database should not open")

    monkeypatch.setattr(cli.JobIntelDatabase, "connect", fail_if_opened)
    result = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python",
            "--salary-min",
            "40",
            "--salary-max",
            "20",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_DISCOVERY_REQUEST"
    assert opened is False


def test_discover_checks_analysis_provider_before_database_or_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    def fail_provider(_settings: object) -> object:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    def fail_if_opened(_path: object) -> JobIntelDatabase:
        nonlocal opened
        opened = True
        raise AssertionError("database and sources must not be touched")

    monkeypatch.setattr(cli, "build_jobintel_provider", fail_provider)
    monkeypatch.setattr(cli.JobIntelDatabase, "connect", fail_if_opened)
    result = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python",
            "--analyze-top",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "ANALYSIS_PROVIDER_UNAVAILABLE"
    assert opened is False


def test_source_doctor_reports_browser_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "cdp_reachable", lambda _port: False)

    result = runner.invoke(cli.app, ["source-doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sources"][0] == {
        "message": "run jobintel setup-browser",
        "mode": "local_chrome",
        "ready": False,
        "source": "boss",
    }


def test_outreach_cli_generates_reviews_and_lists_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "outreach.db"
    database, repository = _seeded_repository(db_path)
    job, _profile, analysis = build_outreach_scope(repository)
    repository.save_analysis(analysis)
    database.close()
    provider = FakeProvider(
        [
            TurnResult(
                tool_calls=[
                    ToolCall(
                        id="outreach",
                        name=OUTREACH_SUBMIT_TOOL,
                        arguments=valid_outreach_message(job).model_dump(),
                    )
                ]
            )
        ]
    )
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(cli, "build_jobintel_provider", lambda _settings: provider)

    generated = runner.invoke(
        cli.app,
        ["outreach", "generate", "--analysis-id", analysis.analysis_id, "--json"],
    )
    assert generated.exit_code == 0, generated.stdout
    outreach_id = json.loads(generated.stdout)["outreach"]["outreach_id"]

    approved = runner.invoke(cli.app, ["outreach", "approve", outreach_id, "--json"])
    sent = runner.invoke(cli.app, ["outreach", "mark-sent", outreach_id, "--json"])
    listed = runner.invoke(cli.app, ["outreach", "list", "--json"])
    shown = runner.invoke(cli.app, ["outreach", "show", outreach_id, "--json"])

    assert json.loads(approved.stdout)["status"] == "approved"
    assert json.loads(sent.stdout)["status"] == "sent_confirmed"
    assert json.loads(listed.stdout)[0]["outreach_id"] == outreach_id
    assert [event["event_type"] for event in json.loads(shown.stdout)["events"]] == [
        "approved",
        "sent_confirmed",
    ]


def test_notify_cli_emails_saved_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "notify.db"
    sender = _EmailSender()
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(db_path))
    monkeypatch.setattr(
        cli,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _DiscoveryConnector()},
    )
    monkeypatch.setattr(
        cli,
        "build_email_sender",
        lambda _settings, *, recipient: sender if recipient == "owner@example.net" else None,
    )
    discovered = runner.invoke(
        cli.app,
        [
            "discover",
            "--candidate-id",
            "C001",
            "--query",
            "Python",
            "--limit",
            "10",
            "--json",
        ],
    )
    run_id = json.loads(discovered.stdout)["discovery"]["run_id"]
    database = JobIntelDatabase.connect(db_path)
    repository = SQLiteJobRepository(database)
    now = datetime.now(UTC)
    repository.save_candidate_email_preference(
        CandidateEmailPreference(
            candidate_id="C001",
            recipient_email="owner@example.net",
            created_at=now,
            updated_at=now,
        )
    )
    database.close()

    notified = runner.invoke(cli.app, ["notify", "discovery", run_id, "--limit", "10", "--json"])

    assert notified.exit_code == 0, notified.stdout
    assert json.loads(notified.stdout)["status"] == "sent"
    assert len(sender.messages) == 1
    plain = sender.messages[0].get_body(preferencelist=("plain",))
    assert plain is not None
    assert "Python 后端工程师" in plain.get_content()
