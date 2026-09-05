"""Offline HTTP tests for the local JobIntel browser workspace."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import FakeProvider

import jobintel.web.app as web_module
from jobintel.config import JobIntelProviderName, JobIntelSettings
from jobintel.discovery.models import JobSource, RawJobListing
from jobintel.providers.base import ToolCall, TurnResult, Usage
from jobintel.services.resume_parser import RESUME_SUBMIT_TOOL
from jobintel.web.app import create_app


class _Connector:
    source = JobSource.BOSS

    def search(self, _preference: object, *, limit: int) -> tuple[RawJobListing, ...]:
        return (
            RawJobListing(
                source=JobSource.BOSS,
                external_id="web-boss-1",
                title="Agent 开发实习",
                company_name="智能科技",
                location="北京 · 海淀",
                salary_text="250-350元/天",
                description="Python RAG Agent 实习",
                experience="在校/应届",
                education="本科",
                url="https://www.zhipin.com/job_detail/web-boss-1.html",
                published_text="今天",
            ),
        )[:limit]


def _settings(path: Path) -> JobIntelSettings:
    return JobIntelSettings(
        jobintel_db_path=path,
        llm_provider=JobIntelProviderName.DEEPSEEK,
        deepseek_api_key="test-key",
    )


def _resume_provider() -> FakeProvider:
    return FakeProvider(
        [
            TurnResult(
                tool_calls=[
                    ToolCall(
                        id="resume",
                        name=RESUME_SUBMIT_TOOL,
                        arguments={
                            "summary": "具备 Python 和 Agent 项目经验的候选人",
                            "evidence": [
                                {
                                    "evidence_type": "project",
                                    "title": "Agent 项目",
                                    "content": "使用 Python 开发了 RAG Agent。",
                                    "skills": ["Python", "RAG", "Agent"],
                                }
                            ],
                        },
                    )
                ],
                usage=Usage(input_tokens=20, output_tokens=10),
            )
        ]
    )


def test_web_dashboard_and_static_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_module, "cdp_reachable", lambda _port: True)
    client = TestClient(create_app(_settings(tmp_path / "web.db")))

    index = client.get("/")
    dashboard = client.get("/api/dashboard")
    health = client.get("/api/health")

    assert index.status_code == 200
    assert "求职情报工作台" in index.text
    assert dashboard.status_code == 200
    assert dashboard.json()["counts"]["candidates"] == 2
    assert health.json()["boss_browser_ready"] is True
    assert health.json()["provider"] == "deepseek"


def test_web_resume_preview_and_confirm_are_two_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "web.db"
    monkeypatch.setattr(web_module, "build_jobintel_provider", lambda _settings: _resume_provider())
    client = TestClient(create_app(_settings(database_path)))

    previewed = client.post(
        "/api/profiles/preview",
        data={"candidate_id": "C001", "provider": "deepseek"},
        files={"resume": ("resume.txt", "Python Agent 项目经验, 完成 RAG 服务开发与评测。")},
    )

    assert previewed.status_code == 200, previewed.text
    preview = previewed.json()
    assert preview["persisted"] is False
    assert preview["preview"]["profile_version"] == 3
    assert client.get("/api/profiles/C001").json()["profile_version"] == 2

    confirmed = client.post("/api/profiles/confirm", json={"preview_id": preview["preview_id"]})

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["profile_version"] == 3
    assert client.get("/api/profiles/C001").json()["profile_version"] == 3


def test_web_discovery_analysis_and_radar_reuse_core_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        web_module,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _Connector()},
    )
    client = TestClient(create_app(_settings(tmp_path / "web.db")))
    discovered = client.post(
        "/api/discoveries",
        json={
            "candidate_id": "C001",
            "query": "Agent 开发实习",
            "city": "北京",
            "daily_salary_min_yuan": 200,
            "employment_types": ["internship"],
            "limit": 20,
            "detail_top": 0,
        },
    )

    assert discovered.status_code == 200, discovered.text
    run = discovered.json()["discovery"]
    assert run["hits"][0]["job"]["salary_daily_min_yuan"] == 250
    run_id = run["run_id"]
    assert client.get(f"/api/discoveries/{run_id}").status_code == 200

    monkeypatch.setattr(web_module, "build_jobintel_provider", lambda _settings: object())
    monkeypatch.setattr(
        web_module,
        "analyze_discovery_hits",
        lambda saved_run, **_kwargs: [
            {
                "discovery_job_id": saved_run.hits[0].job.discovery_job_id,
                "error": {"message": "scripted"},
            }
        ],
    )
    analyzed = client.post(f"/api/discoveries/{run_id}/analyze", json={"top": 1})
    assert analyzed.status_code == 200
    assert analyzed.json()["run_id"] == run_id

    checked = client.post(
        "/api/radar/checks",
        json={"baseline_run_id": run_id, "detail_top": 0, "force": True},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["events"][0]["status"] == "unchanged"
    assert client.get("/api/radar/checks").json()[0]["run_id"] == checked.json()["run_id"]


def test_web_rejects_invalid_preview_and_mixed_salary_units(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path / "web.db")))

    traversal = client.post("/api/profiles/confirm", json={"preview_id": "../../outside.json"})
    mixed = client.post(
        "/api/discoveries",
        json={
            "candidate_id": "C001",
            "query": "Python",
            "salary_min_k": 20,
            "daily_salary_min_yuan": 200,
        },
    )

    assert traversal.status_code == 400
    assert traversal.json()["detail"]["code"] == "PROFILE_CONFIRM_FAILED"
    assert mixed.status_code == 400
    assert "cannot be mixed" in mixed.json()["detail"]["message"]
