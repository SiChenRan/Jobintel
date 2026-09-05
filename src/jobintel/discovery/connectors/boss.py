"""BOSS直聘 discovery through a user-controlled local Chrome session."""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode

from jobintel.discovery.connectors.base import (
    AuthenticationRequiredError,
    SourceBlockedError,
    SourceUnavailableError,
)
from jobintel.discovery.connectors.cdp import DEFAULT_CDP_PORT, CDPSession
from jobintel.discovery.models import (
    DetailFetchResult,
    DetailFetchStatus,
    EmploymentType,
    JobSearchPreference,
    JobSource,
    JobSourceLink,
    RawJobDetail,
    RawJobListing,
    content_digest,
    infer_employment_type,
    parse_company_size,
    utc_now,
)

BOSS_ORIGIN = "https://www.zhipin.com"
BOSS_SEARCH_PAGE = f"{BOSS_ORIGIN}/web/geek/job"
BOSS_API_PATH = "/wapi/zpgeek/search/joblist.json"
_BOSS_PAGE_SIZE = 30
BOSS_CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "重庆": "101040100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "南京": "101190100",
    "苏州": "101190400",
    "武汉": "101200100",
    "成都": "101270100",
    "西安": "101110100",
}

_FETCH_EXPRESSION = r"""(async () => {
  const response = await fetch(__URL__, {
    credentials: "include", headers: {Accept: "application/json"}
  });
  if (response.status === 401 || response.status === 403) {
    return JSON.stringify({error: "authentication_required", status: response.status});
  }
  const text = await response.text();
  if (text.trimStart().startsWith("<")) {
    return JSON.stringify({error: "blocked", status: response.status});
  }
  if (!response.ok) {
    return JSON.stringify({error: "http_error", status: response.status});
  }
  const data = JSON.parse(text);
  if (data.code !== undefined && Number(data.code) !== 0) {
    const message = String(data.message || data.zpData || "");
    return JSON.stringify({
      error: /登录|login/i.test(message) ? "authentication_required" : "blocked",
      status: response.status,
      message
    });
  }
  if (!data.zpData || !Array.isArray(data.zpData.jobList)) {
    return JSON.stringify({error: "blocked", status: response.status});
  }
  const jobs = data.zpData.jobList.map((job) => ({
    external_id: job.encryptJobId || job.securityId || "",
    title: job.jobName || "",
    company_name: job.brandName || "",
    location: [job.cityName, job.areaDistrict, job.businessDistrict]
      .filter((value) => value && value !== "不限").join(" · "),
    salary_text: job.salaryDesc || "",
    description: [
      ...(job.skills || []), ...(job.jobLabels || []), ...(job.welfareList || [])
    ].join(", "),
    experience: job.jobExperience || "",
    education: job.jobDegree || "",
    employment_type: job.jobTypeName || "",
    company_size: job.brandScaleName || "",
    url: job.encryptJobId
      ? `https://www.zhipin.com/job_detail/${job.encryptJobId}.html` : "",
    published_text: job.activeTimeDesc || ""
  }));
  return JSON.stringify({jobs});
})()"""

_DETAIL_EXPRESSION = r"""(async () => {
  const response = await fetch(__URL__, {
    credentials: "include", headers: {Accept: "text/html"}, redirect: "follow"
  });
  const text = await response.text();
  const finalUrl = response.url || __URL__;
  if (response.status === 401 || response.status === 403 || /\/web\/user\//.test(finalUrl)) {
    return JSON.stringify({error: "authentication_required", status: response.status});
  }
  const documentCopy = new DOMParser().parseFromString(text, "text/html");
  if (/security-check|captcha|verify/i.test(finalUrl)
      || documentCopy.querySelector(".security-check, .verify-box, .captcha-box")) {
    return JSON.stringify({error: "blocked", status: response.status});
  }
  if (!response.ok) {
    return JSON.stringify({error: "http_error", status: response.status});
  }
  const textOf = (selector) => {
    const node = documentCopy.querySelector(selector);
    return node ? (node.innerText || node.textContent || "").trim() : "";
  };
  const textsOf = (selector) => Array.from(documentCopy.querySelectorAll(selector))
    .map((node) => (node.innerText || node.textContent || "").trim()).filter(Boolean);
  const sections = textsOf(".job-sec-text");
  const description = sections[0] || "";
  if (!description) {
    return JSON.stringify({error: "detail_unavailable", status: response.status});
  }
  const recruiterBlock = textOf(".job-boss-info");
  const recruiterLines = recruiterBlock.split(/\n+/).map((value) => value.trim()).filter(Boolean);
  const recruiterNameLines = textOf(".job-boss-info .name")
    .split(/\n+/).map((value) => value.trim()).filter(Boolean);
  return JSON.stringify({detail: {
    description,
    skills: textsOf(
      ".job-detail-section .job-tags li, .job-detail-section .job-tags span"
    ).slice(0, 100),
    company_description: sections[1] || "",
    recruiter_name: recruiterNameLines.find((value) => !/活跃|在线/.test(value))
      || recruiterLines[0] || "",
    recruiter_title: textOf(".job-boss-info .boss-info-attr") || recruiterLines.at(-1) || "",
    recruiter_active_text: textOf(".job-boss-info .boss-active-time")
      || recruiterLines.find((value) => /活跃|在线/.test(value)) || ""
  }});
})()"""


class BossConnector:
    """Search BOSS in the origin page without exporting browser credentials."""

    source = JobSource.BOSS

    def __init__(
        self,
        *,
        cdp_port: int = DEFAULT_CDP_PORT,
        session_factory: Callable[[int], CDPSession] = CDPSession,
        search_min_delay_seconds: float = 1.2,
        search_max_delay_seconds: float = 2.4,
        detail_min_delay_seconds: float = 3.0,
        detail_max_delay_seconds: float = 6.0,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        """Configure the local CDP endpoint and injectable session factory."""
        if search_min_delay_seconds < 0 or search_min_delay_seconds > search_max_delay_seconds:
            raise ValueError("invalid BOSS search delay range")
        if detail_min_delay_seconds < 0 or detail_min_delay_seconds > detail_max_delay_seconds:
            raise ValueError("invalid BOSS detail delay range")
        self._port = cdp_port
        self._session_factory = session_factory
        self._search_delay = (search_min_delay_seconds, search_max_delay_seconds)
        self._detail_delay = (detail_min_delay_seconds, detail_max_delay_seconds)
        self._sleep = sleeper
        self._jitter = jitter

    def search(self, preference: JobSearchPreference, *, limit: int) -> tuple[RawJobListing, ...]:
        """Fetch bounded BOSS result pages through the logged-in browser."""
        cdp = self._session_factory(self._port)
        target_id: str | None = None
        try:
            target_id = cdp.send("Target.createTarget", {"url": "about:blank", "background": True})[
                "result"
            ]["targetId"]
            session_id = cdp.send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )["result"]["sessionId"]
            cdp.send("Page.enable", session_id=session_id)
            cdp.send("Runtime.enable", session_id=session_id)
            cdp.send("Page.navigate", {"url": BOSS_SEARCH_PAGE}, session_id)
            self._wait_ready(cdp, session_id)
            listings: list[RawJobListing] = []
            source_query = self._source_query(preference)
            for page in range(1, min(10, math.ceil(limit / _BOSS_PAGE_SIZE)) + 1):
                params: dict[str, str | int] = {
                    "scene": 1,
                    "query": source_query,
                    "page": page,
                    # BOSS derives page offsets from pageSize. Changing it on the
                    # last page makes that page overlap the previous one.
                    "pageSize": _BOSS_PAGE_SIZE,
                }
                if preference.city:
                    params["city"] = BOSS_CITY_CODES.get(preference.city, preference.city)
                api_url = f"{BOSS_ORIGIN}{BOSS_API_PATH}?{urlencode(params)}"
                raw = cdp.evaluate(
                    _FETCH_EXPRESSION.replace("__URL__", json.dumps(api_url)), session_id
                )
                page_items = self._parse(raw)
                listings.extend(self._to_listing(item) for item in page_items)
                if len(page_items) < _BOSS_PAGE_SIZE or len(listings) >= limit:
                    break
                self._sleep(self._jitter(*self._search_delay))
            return tuple(listings[:limit])
        finally:
            if target_id is not None:
                with suppress(Exception):
                    cdp.send("Target.closeTarget", {"targetId": target_id})
            cdp.close()

    @staticmethod
    def _source_query(preference: JobSearchPreference) -> str:
        """Add an explicit source keyword for employment types BOSS may ignore."""
        query = preference.query.strip()
        normalized = query.casefold()
        suffix = ""
        if EmploymentType.INTERNSHIP in preference.employment_types and not any(
            marker in normalized for marker in ("实习", "intern")
        ):
            suffix = "实习"
        elif EmploymentType.PART_TIME in preference.employment_types and "兼职" not in query:
            suffix = "兼职"
        return f"{query} {suffix}".strip()

    def fetch_details(self, links: tuple[JobSourceLink, ...]) -> tuple[DetailFetchResult, ...]:
        """Fetch at most ten details serially, with jitter and a risk-control circuit breaker."""
        if len(links) > 10:
            raise ValueError("BOSS detail batches are limited to 10 jobs")
        if any(link.source is not JobSource.BOSS for link in links):
            raise ValueError("BOSS connector received a non-BOSS detail link")
        if not links:
            return ()

        cdp = self._session_factory(self._port)
        target_id: str | None = None
        results: list[DetailFetchResult] = []
        try:
            target_id = cdp.send(
                "Target.createTarget", {"url": BOSS_SEARCH_PAGE, "background": True}
            )["result"]["targetId"]
            session_id = cdp.send(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )["result"]["sessionId"]
            cdp.send("Page.enable", session_id=session_id)
            cdp.send("Runtime.enable", session_id=session_id)
            self._wait_ready(cdp, session_id)
            for link in links:
                started = time.perf_counter()
                self._sleep(self._jitter(*self._detail_delay))
                try:
                    raw = cdp.evaluate(
                        _DETAIL_EXPRESSION.replace("__URL__", json.dumps(str(link.url))),
                        session_id,
                    )
                    payload = self._parse_detail(raw)
                    detail = self._to_detail(link, payload)
                except AuthenticationRequiredError as exc:
                    results.append(
                        self._detail_failure(
                            link, DetailFetchStatus.AUTHENTICATION_REQUIRED, started, exc
                        )
                    )
                    break
                except SourceBlockedError as exc:
                    results.append(
                        self._detail_failure(link, DetailFetchStatus.BLOCKED, started, exc)
                    )
                    break
                except SourceUnavailableError as exc:
                    results.append(
                        self._detail_failure(link, DetailFetchStatus.UNAVAILABLE, started, exc)
                    )
                    break
                except Exception as exc:  # one unknown source response also opens the breaker
                    results.append(
                        self._detail_failure(link, DetailFetchStatus.FAILED, started, exc)
                    )
                    break
                results.append(
                    DetailFetchResult(
                        source=JobSource.BOSS,
                        external_id=link.external_id,
                        status=DetailFetchStatus.SUCCESS,
                        elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                        detail=detail,
                    )
                )
            return tuple(results)
        finally:
            if target_id is not None:
                with suppress(Exception):
                    cdp.send("Target.closeTarget", {"targetId": target_id})
            cdp.close()

    @staticmethod
    def _wait_ready(cdp: CDPSession, session_id: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cdp.evaluate("document.readyState", session_id) in {"interactive", "complete"}:
                return
            time.sleep(0.15)
        raise SourceUnavailableError("BOSS page did not become ready")

    @staticmethod
    def _parse(raw: Any) -> list[dict[str, Any]]:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise SourceBlockedError("BOSS returned malformed data") from exc
        error = payload.get("error")
        if error == "authentication_required":
            raise AuthenticationRequiredError("请在专用 Chrome 中登录 BOSS 后重试")
        if error == "blocked":
            raise SourceBlockedError("BOSS 要求完成浏览器安全验证")
        if error:
            raise SourceBlockedError(f"BOSS search failed: {error}")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise SourceBlockedError("BOSS response does not contain a jobs list")
        return [item for item in jobs if isinstance(item, dict) and item.get("url")]

    @staticmethod
    def _parse_detail(raw: Any) -> dict[str, Any]:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise SourceBlockedError("BOSS returned malformed job detail") from exc
        error = payload.get("error")
        if error == "authentication_required":
            raise AuthenticationRequiredError("请在专用 Chrome 中重新登录 BOSS 后再抓取详情")
        if error == "blocked":
            raise SourceBlockedError("BOSS 要求安全验证; 详情抓取已立即停止")
        if error == "detail_unavailable":
            raise SourceUnavailableError("职位详情不存在、已下线或页面结构已变化")
        if error:
            raise SourceBlockedError(f"BOSS detail failed: {error}")
        detail = payload.get("detail")
        if not isinstance(detail, dict) or not detail.get("description"):
            raise SourceBlockedError("BOSS response does not contain a job description")
        return detail

    @staticmethod
    def _to_detail(link: JobSourceLink, payload: dict[str, Any]) -> RawJobDetail:
        def clean(field: str) -> str:
            return " ".join(str(payload.get(field, "")).split())

        values = {
            "description": clean("description"),
            "skills": tuple(
                dict.fromkeys(
                    " ".join(str(value).split())
                    for value in payload.get("skills", [])
                    if " ".join(str(value).split())
                )
            ),
            "company_description": clean("company_description"),
            "recruiter_name": clean("recruiter_name"),
            "recruiter_title": clean("recruiter_title"),
            "recruiter_active_text": clean("recruiter_active_text"),
        }
        return RawJobDetail(
            source=JobSource.BOSS,
            external_id=link.external_id,
            url=link.url,
            fetched_at=utc_now(),
            content_sha256=content_digest(values),
            **values,
        )

    @staticmethod
    def _detail_failure(
        link: JobSourceLink,
        status: DetailFetchStatus,
        started: float,
        exc: Exception,
    ) -> DetailFetchResult:
        return DetailFetchResult(
            source=JobSource.BOSS,
            external_id=link.external_id,
            status=status,
            elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
            message=(" ".join(str(exc).split())[:500] or exc.__class__.__name__),
        )

    @staticmethod
    def _to_listing(item: dict[str, Any]) -> RawJobListing:
        values = dict(item)
        source_employment_type = str(values.pop("employment_type", ""))
        source_company_size = str(values.pop("company_size", ""))
        values["employment_type"] = infer_employment_type(
            source_employment_type,
            str(values.get("title", "")),
            str(values.get("description", "")),
        )
        values["company_size"] = parse_company_size(source_company_size)
        return RawJobListing(source=JobSource.BOSS, **values)
