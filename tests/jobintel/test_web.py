"""Offline HTTP tests for the local JobIntel browser workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.conftest import FakeProvider
from tests.jobintel.outreach_fixtures import build_outreach_scope, valid_outreach_message

import jobintel.web.app as web_module
from jobintel.config import JobIntelProviderName, JobIntelSettings
from jobintel.discovery.models import CompanySize, EmploymentType, JobSource, RawJobListing
from jobintel.outreach.service import OUTREACH_SUBMIT_TOOL
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
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
                company_size=CompanySize.SMALL,
                url="https://www.zhipin.com/job_detail/web-boss-1.html",
                published_text="今天",
            ),
        )[:limit]


@dataclass
class _EmailSender:
    from_address: str = "jobs@example.com"
    recipient: str = "owner@example.net"
    messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


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


def test_web_discovery_uses_student_friendly_defaults_and_company_size_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class ObservedConnector(_Connector):
        def search(self, preference: object, *, limit: int) -> tuple[RawJobListing, ...]:
            observed["preference"] = preference
            observed["limit"] = limit
            return super().search(preference, limit=limit)

    monkeypatch.setattr(
        web_module,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: ObservedConnector()},
    )
    client = TestClient(create_app(_settings(tmp_path / "defaults.db")))

    response = client.post(
        "/api/discoveries",
        json={"candidate_id": "C001", "company_sizes": ["small"], "detail_top": 0},
    )

    assert response.status_code == 200, response.text
    run = response.json()["discovery"]
    assert run["preference"]["query"] == "Agent开发"
    assert run["preference"]["city"] == "北京"
    assert run["preference"]["employment_types"] == [EmploymentType.INTERNSHIP.value]
    assert run["preference"]["limit"] == 10
    assert run["preference"]["company_sizes"] == [CompanySize.SMALL.value]
    assert run["hits"][0]["job"]["company_size"] == CompanySize.SMALL.value
    assert observed["limit"] == 30


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


def test_web_outreach_workflow_enforces_revision_and_manual_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "outreach-web.db"
    client = TestClient(create_app(_settings(database_path)))
    assert client.get("/api/dashboard").status_code == 200
    database = JobIntelDatabase.connect(database_path)
    repository = SQLiteJobRepository(database)
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
    monkeypatch.setattr(web_module, "build_jobintel_provider", lambda _settings: provider)

    generated = client.post(
        f"/api/analyses/{analysis.analysis_id}/outreach-drafts",
        json={"tone": "professional", "focus_requirement_ids": []},
    )
    assert generated.status_code == 200, generated.text
    payload = generated.json()
    outreach_id = payload["outreach"]["outreach_id"]
    assert payload["outreach"]["status"] == "draft"
    assert payload["citations"][0]["evidence"][0]["content"]
    assert payload["job"]["source_url"].startswith("https://")

    revised = client.post(
        f"/api/outreach-drafts/{outreach_id}/revisions",
        json={"revision": 1, "message": "招聘负责人您好\n<script>测试</script>人工修改文案"},
    )
    assert revised.status_code == 200
    assert revised.json()["outreach"]["revision"] == 2
    stale = client.post(f"/api/outreach-drafts/{outreach_id}/approve", json={"revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "OUTREACH_STATE_CONFLICT"

    approved = client.post(f"/api/outreach-drafts/{outreach_id}/approve", json={"revision": 2})
    copied = client.post(f"/api/outreach-drafts/{outreach_id}/events/copied", json={"revision": 2})
    opened = client.post(f"/api/outreach-drafts/{outreach_id}/events/opened", json={"revision": 2})
    sent = client.post(
        f"/api/outreach-drafts/{outreach_id}/events/sent-confirmed",
        json={"revision": 2},
    )
    assert approved.json()["outreach"]["status"] == "approved"
    assert copied.status_code == 200
    assert opened.status_code == 200
    assert sent.json()["outreach"]["status"] == "sent_confirmed"
    assert [item["event_type"] for item in sent.json()["events"]] == [
        "approved",
        "copied",
        "opened",
        "sent_confirmed",
    ]
    assert (
        client.get(f"/api/outreach-drafts?analysis_id={analysis.analysis_id}").json()[0][
            "outreach"
        ]["revision"]
        == 2
    )


def test_web_outreach_ui_escapes_dynamic_content_and_has_no_auto_send(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(_settings(tmp_path / "static.db")))
    script = client.get("/static/app.js").text
    shell = client.get("/").text

    assert "escapeHtml(draft.effective_message)" in script
    assert "navigator.clipboard.writeText" in script
    assert "window.open" in script
    assert "events/sent-confirmed" in script
    assert 'id="notification-email-form"' in shell
    assert "/notification-email" in script
    assert "仅用于匹配排序" in script
    assert "当前筛选" in script
    assert 'id="discovery-history"' in shell
    assert 'name="query" required value="Agent开发"' in shell
    assert 'name="city" value="北京"' in shell
    assert 'value="internship" selected' in shell
    assert 'name="company_size"' in shell
    assert 'name="limit" type="number" min="1" max="500" value="10"' in shell
    assert "openDiscoveryRun" in script
    assert "无职位可发送" in script
    assert "邮件服务未就绪" in script
    assert "不计分 · 定性参考" in script
    assert "本次分数仅计算" in script
    assert "const form = event.currentTarget;" in script
    assert "event.currentTarget.elements" not in script
    assert "自动发送" not in shell
    assert "数据只保存在这台服务器" not in shell


def test_web_saves_candidate_recipient_and_emails_its_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path / "email-web.db").model_copy(
        update={
            "smtp_host": "smtp.example.com",
            "smtp_from_address": "jobs@example.com",
        }
    )
    sender = _EmailSender()
    monkeypatch.setattr(
        web_module,
        "build_connectors",
        lambda **_kwargs: {JobSource.BOSS: _Connector()},
    )
    monkeypatch.setattr(
        web_module,
        "build_email_sender",
        lambda _settings, *, recipient: sender if recipient == "owner@example.net" else None,
    )
    client = TestClient(create_app(settings))
    discovered = client.post(
        "/api/discoveries",
        json={"candidate_id": "C001", "query": "Agent 实习", "limit": 10},
    ).json()["discovery"]

    missing = client.post(
        f"/api/discoveries/{discovered['run_id']}/notifications/email",
        json={"limit": 10},
    )
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "EMAIL_RECIPIENT_NOT_CONFIGURED"

    saved = client.put(
        "/api/profiles/C001/notification-email",
        json={"recipient_email": "owner@example.net"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["recipient_masked"] == "o***@example.net"
    preference = client.get("/api/profiles/C001/notification-email").json()
    assert preference == {
        "candidate_id": "C001",
        "configured": True,
        "recipient_masked": "o***@example.net",
    }
    assert "owner@example.net" not in str(preference)

    sent = client.post(
        f"/api/discoveries/{discovered['run_id']}/notifications/email",
        json={"limit": 10},
    )

    assert sent.status_code == 200, sent.text
    assert sent.json()["recipient_masked"] == "o***@example.net"
    assert sent.json()["status"] == "sent"
    assert len(sender.messages) == 1
    assert sender.messages[0]["To"] == "owner@example.net"
    assert client.get("/api/health").json()["smtp_notification_ready"] is True
    arbitrary_recipient = client.post(
        f"/api/discoveries/{discovered['run_id']}/notifications/email",
        json={"limit": 10, "recipient": "other@example.org"},
    )
    assert arbitrary_recipient.status_code == 422
