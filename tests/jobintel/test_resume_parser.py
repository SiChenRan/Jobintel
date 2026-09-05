"""Resume ingestion tests keep profile creation offline and explicitly two-phase."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import FakeProvider
from typer.testing import CliRunner

from jobintel import cli
from jobintel.models import EvidenceType
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database
from jobintel.providers.base import ToolCall, TurnResult, Usage
from jobintel.services.resume_parser import (
    RESUME_SUBMIT_TOOL,
    CandidateProfilePreview,
    ResumeParserService,
    materialize_profile,
    read_resume_text,
)

runner = CliRunner()


def _resume_turn() -> TurnResult:
    return TurnResult(
        tool_calls=[
            ToolCall(
                id="resume-profile",
                name=RESUME_SUBMIT_TOOL,
                arguments={
                    "summary": "Python 后端与智能体开发候选人",
                    "evidence": [
                        {
                            "evidence_type": "experience",
                            "title": "后端开发实习",
                            "content": "使用 FastAPI 开发接口, 并将延迟降低 30%。",
                            "skills": ["Python", "FastAPI"],
                        },
                        {
                            "evidence_type": "project",
                            "title": "RAG 项目",
                            "content": "实现检索增强生成服务与离线评测流程。",
                            "skills": ["RAG", "LLM"],
                        },
                    ],
                },
            )
        ],
        usage=Usage(input_tokens=100, output_tokens=50),
    )


def test_read_resume_text_and_materialize_stable_evidence(tmp_path: Path) -> None:
    resume = tmp_path / "resume.md"
    resume.write_text("# 张三\n\nPython 后端开发, 拥有 FastAPI 和 RAG 项目经验。", encoding="utf-8")

    text, digest = read_resume_text(resume)

    assert "FastAPI" in text
    assert len(digest) == 64

    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_bytes(b"this is not a PDF document")
    with pytest.raises(ValueError, match="提取失败"):
        read_resume_text(invalid_pdf)


@pytest.mark.asyncio
async def test_resume_preview_has_no_persistence_side_effect(tmp_path: Path) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text(
        "候选人具有 Python、FastAPI 与 RAG 项目开发经验, 负责接口与评测。", encoding="utf-8"
    )
    provider = FakeProvider([_resume_turn()])

    result = await ResumeParserService(provider).preview(
        candidate_id="C900",
        profile_version=1,
        resume_file=resume,
    )
    profile = materialize_profile(result.preview)

    assert result.preview.candidate_id == "C900"
    assert result.telemetry.attempts == 1
    assert profile.evidence[0].evidence_type is EvidenceType.EXPERIENCE
    assert (
        profile.evidence[0].evidence_id
        == materialize_profile(result.preview).evidence[0].evidence_id
    )


def test_profile_import_then_confirm_creates_one_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "jobintel.db"
    resume = tmp_path / "resume.txt"
    preview_path = tmp_path / "preview.json"
    resume.write_text(
        "候选人具有 Python、FastAPI 与 RAG 项目开发经验, 负责接口与评测。", encoding="utf-8"
    )
    database = JobIntelDatabase.connect(database_path)
    seed_database(database)
    before = database.connection.execute(
        "SELECT COUNT(*) FROM candidate_profiles WHERE candidate_id = 'C001'"
    ).fetchone()[0]
    database.close()

    monkeypatch.setenv("JOBINTEL_DB_PATH", str(database_path))
    monkeypatch.setattr(
        cli,
        "build_jobintel_provider",
        lambda _settings: FakeProvider([_resume_turn()]),
    )
    imported = runner.invoke(
        cli.app,
        [
            "profile",
            "import",
            "--candidate-id",
            "C001",
            "--resume-file",
            str(resume),
            "--preview-file",
            str(preview_path),
            "--json",
        ],
    )

    assert imported.exit_code == 0, imported.stdout
    payload = json.loads(imported.stdout)
    assert payload["persisted"] is False
    assert payload["preview"]["profile_version"] == 3
    preview = CandidateProfilePreview.model_validate_json(preview_path.read_text(encoding="utf-8"))
    assert preview.profile_version == 3
    database = JobIntelDatabase.connect(database_path)
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM candidate_profiles WHERE candidate_id = 'C001'"
        ).fetchone()[0]
        == before
    )
    database.close()

    confirmed = runner.invoke(cli.app, ["profile", "confirm", str(preview_path), "--json"])

    assert confirmed.exit_code == 0, confirmed.stdout
    assert json.loads(confirmed.stdout)["profile_version"] == 3
    database = JobIntelDatabase.connect(database_path)
    repository = SQLiteJobRepository(database)
    assert repository.get_candidate_profile("C001").profile_version == 3
    database.close()


def test_confirm_rejects_stale_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "jobintel.db"
    resume = tmp_path / "resume.txt"
    preview_path = tmp_path / "preview.json"
    resume.write_text(
        "候选人具有 Python、FastAPI 与 RAG 项目开发经验, 负责接口与评测。", encoding="utf-8"
    )
    monkeypatch.setenv("JOBINTEL_DB_PATH", str(database_path))
    monkeypatch.setattr(
        cli,
        "build_jobintel_provider",
        lambda _settings: FakeProvider([_resume_turn()]),
    )
    imported = runner.invoke(
        cli.app,
        [
            "profile",
            "import",
            "--candidate-id",
            "C001",
            "--resume-file",
            str(resume),
            "--preview-file",
            str(preview_path),
            "--json",
        ],
    )
    assert imported.exit_code == 0
    first = runner.invoke(cli.app, ["profile", "confirm", str(preview_path), "--json"])
    second = runner.invoke(cli.app, ["profile", "confirm", str(preview_path), "--json"])

    assert first.exit_code == 0
    assert second.exit_code == 1
    assert json.loads(second.stdout)["error"]["code"] == "PROFILE_CONFIRM_FAILED"
