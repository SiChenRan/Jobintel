from __future__ import annotations

import json
from typing import Any

import pytest

from jobintel.discovery.connectors.base import AuthenticationRequiredError
from jobintel.discovery.connectors.boss import BossConnector
from jobintel.discovery.models import (
    CompanySize,
    DetailFetchStatus,
    EmploymentType,
    JobSearchPreference,
    JobSource,
    JobSourceLink,
)


class FakeCDP:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.closed = False
        self.sent: list[str] = []
        self.expressions: list[str] = []

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append(method)
        if method == "Target.createTarget":
            return {"result": {"targetId": "target"}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "session"}}
        return {"result": {}}

    def evaluate(self, expression: str, session_id: str) -> object:
        if expression == "document.readyState":
            return "complete"
        self.expressions.append(expression)
        return json.dumps(self.payload)

    def close(self) -> None:
        self.closed = True


def _preference() -> JobSearchPreference:
    return JobSearchPreference(
        candidate_id="C001",
        profile_version=2,
        query="Python 后端",
        city="上海",
        limit=10,
    )


def test_boss_connector_uses_local_page_context_and_maps_results() -> None:
    cdp = FakeCDP(
        {
            "jobs": [
                {
                    "external_id": "boss-1",
                    "title": "Python工程师",
                    "company_name": "直聘科技",
                    "location": "上海",
                    "salary_text": "25-40K",
                    "description": "Python",
                    "experience": "3-5年",
                    "education": "本科",
                    "company_size": "20-99人",
                    "url": "https://www.zhipin.com/job_detail/boss-1.html",
                    "published_text": "今日活跃",
                }
            ]
        }
    )

    results = BossConnector(session_factory=lambda _port: cdp).search(_preference(), limit=10)

    assert len(results) == 1
    assert results[0].source is JobSource.BOSS
    assert results[0].company_size is CompanySize.SMALL
    assert "Target.closeTarget" in cdp.sent
    assert cdp.closed is True


def test_boss_connector_keeps_page_size_stable_to_avoid_overlap() -> None:
    class PaginatedCDP(FakeCDP):
        def __init__(self) -> None:
            super().__init__({})
            self.fetch_expressions: list[str] = []

        def evaluate(self, expression: str, session_id: str) -> object:
            if expression == "document.readyState":
                return "complete"
            self.fetch_expressions.append(expression)
            start = 0 if len(self.fetch_expressions) == 1 else 30
            count = 30 if start == 0 else 20
            return json.dumps(
                {
                    "jobs": [
                        {
                            "external_id": f"boss-{index}",
                            "title": f"Python工程师{index}",
                            "company_name": f"公司{index}",
                            "location": "上海",
                            "description": "Python",
                            "url": f"https://www.zhipin.com/job_detail/boss-{index}.html",
                        }
                        for index in range(start, start + count)
                    ]
                }
            )

    cdp = PaginatedCDP()
    results = BossConnector(
        session_factory=lambda _port: cdp,
        search_min_delay_seconds=0,
        search_max_delay_seconds=0,
        sleeper=lambda _seconds: None,
    ).search(_preference().model_copy(update={"limit": 50}), limit=50)

    assert len(results) == 50
    assert len(cdp.fetch_expressions) == 2
    assert all("pageSize=30" in expression for expression in cdp.fetch_expressions)
    assert all("pageSize=20" not in expression for expression in cdp.fetch_expressions)


def test_boss_connector_adds_internship_to_source_query() -> None:
    cdp = FakeCDP({"jobs": []})
    preference = _preference().model_copy(update={"employment_types": (EmploymentType.INTERNSHIP,)})

    BossConnector(session_factory=lambda _port: cdp).search(preference, limit=10)

    assert len(cdp.expressions) == 1
    assert "%E5%AE%9E%E4%B9%A0" in cdp.expressions[0]
    assert cdp.closed is True


def test_boss_connector_reports_missing_login() -> None:
    cdp = FakeCDP({"error": "authentication_required", "status": 401})
    with pytest.raises(AuthenticationRequiredError, match="登录"):
        BossConnector(session_factory=lambda _port: cdp).search(_preference(), limit=10)
    assert cdp.closed is True


def test_boss_detail_fetch_is_serially_delayed_and_mapped() -> None:
    cdp = FakeCDP(
        {
            "detail": {
                "description": "负责 Python 服务与平台稳定性建设。",
                "skills": ["Python", "Redis", "Python"],
                "company_description": "一家技术公司。",
                "recruiter_name": "沈先生",
                "recruiter_title": "研发",
                "recruiter_active_text": "刚刚活跃",
            }
        }
    )
    delays: list[float] = []
    connector = BossConnector(
        session_factory=lambda _port: cdp,
        detail_min_delay_seconds=3,
        detail_max_delay_seconds=6,
        sleeper=delays.append,
        jitter=lambda low, high: (low + high) / 2,
    )
    links = tuple(
        JobSourceLink(
            source=JobSource.BOSS,
            external_id=f"boss-{index}",
            url=f"https://www.zhipin.com/job_detail/boss-{index}.html",
        )
        for index in range(2)
    )

    results = connector.fetch_details(links)

    assert delays == [4.5, 4.5]
    assert [item.status for item in results] == [
        DetailFetchStatus.SUCCESS,
        DetailFetchStatus.SUCCESS,
    ]
    assert results[0].detail is not None
    assert results[0].detail.skills == ("Python", "Redis")
    assert len(results[0].detail.content_sha256) == 64
    assert cdp.closed is True


def test_boss_detail_fetch_opens_circuit_breaker_on_first_block() -> None:
    cdp = FakeCDP({"error": "blocked", "status": 200})
    delays: list[float] = []
    connector = BossConnector(
        session_factory=lambda _port: cdp,
        detail_min_delay_seconds=3,
        detail_max_delay_seconds=6,
        sleeper=delays.append,
        jitter=lambda low, _high: low,
    )
    links = tuple(
        JobSourceLink(
            source=JobSource.BOSS,
            external_id=f"boss-{index}",
            url=f"https://www.zhipin.com/job_detail/boss-{index}.html",
        )
        for index in range(2)
    )

    results = connector.fetch_details(links)

    assert len(results) == 1
    assert results[0].status is DetailFetchStatus.BLOCKED
    assert delays == [3]
