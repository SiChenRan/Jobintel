"""Candidate-aware, deterministic, and explainable discovery ranking."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

from jobintel.discovery.models import (
    DiscoveredJob,
    DiscoveryRankBreakdown,
    JobSearchPreference,
    ProfileEvidenceMatch,
    SearchProfileSnapshot,
)
from jobintel.models import CandidateEvidence, CandidateProfile, EvidenceType


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


_TOKEN_PATTERN = re.compile(r"[a-z0-9+#.]{2,}|[\u4e00-\u9fff]{2,}")
_WEAK_SKILLS = frozenset(
    {
        "skill",
        "skills",
        "开发",
        "编程",
        "技术",
        "项目",
        "软件",
        "计算机",
        "后端",
        "前端",
    }
)
_CONTEXT_STOP_WORDS = _WEAK_SKILLS | frozenset(
    {
        "负责",
        "参与",
        "完成",
        "相关",
        "能力",
        "经验",
        "熟悉",
        "掌握",
        "使用",
        "工作",
        "实习",
        "岗位",
        "候选人",
    }
)
_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("amazon web services", "aws"),
    ("google cloud platform", "gcp"),
    ("kubernetes", "k8s"),
    ("machine learning", "ml", "机器学习"),
    ("postgresql", "postgres", "postgres sql", "pg"),
    ("pytorch", "torch"),
    ("rest api", "restful api", "http api"),
    ("large language model", "llm", "大模型"),
    ("retrieval augmented generation", "rag", "检索增强生成"),
    ("artificial intelligence", "ai", "人工智能"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("golang", "go language", "go语言"),
    ("spring boot", "springboot"),
    ("fastapi", "fast api"),
    ("langchain", "lang chain"),
    ("model context protocol", "mcp"),
)
_NORMALIZED_ALIAS_GROUPS = tuple(
    frozenset(_normalize(value) for value in group) for group in _ALIAS_GROUPS
)
_ROLE_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("agent", "智能体"), ("AI Agent", "大模型应用开发")),
    (("后端", "服务端", "backend"), ("后端开发", "服务端开发")),
    (("算法", "algorithm"), ("AI算法", "机器学习算法")),
    (("数据", "data"), ("数据开发", "数据工程师")),
    (("前端", "frontend"), ("前端开发", "Web前端")),
)


@dataclass(frozen=True)
class _ProfileSkill:
    """One normalized skill and the evidence records supporting it."""

    display: str
    variants: frozenset[str]
    evidence: tuple[CandidateEvidence, ...]


@dataclass(frozen=True)
class CandidateSearchProfile:
    """Internal searchable representation of one immutable candidate profile."""

    snapshot: SearchProfileSnapshot
    skills: tuple[_ProfileSkill, ...]
    evidence: tuple[CandidateEvidence, ...]
    education_text: str


@dataclass(frozen=True)
class ProfileRankingResult:
    """Candidate-aware components consumed by the discovery aggregate."""

    breakdown: DiscoveryRankBreakdown
    matched_query_terms: tuple[str, ...]
    matched_profile_skills: tuple[str, ...]
    matched_evidence: tuple[ProfileEvidenceMatch, ...]


def build_candidate_search_profile(
    profile: CandidateProfile,
    *,
    query: str,
    smart_expand: bool,
) -> CandidateSearchProfile:
    """Build a stable searchable profile without making another LLM call."""
    occurrences: Counter[str] = Counter()
    display_by_key: dict[str, str] = {}
    evidence_by_key: defaultdict[str, list[CandidateEvidence]] = defaultdict(list)
    variants_by_key: dict[str, frozenset[str]] = {}

    for evidence in profile.evidence:
        for raw_skill in evidence.skills:
            normalized = _normalize(raw_skill)
            if not normalized or normalized in _WEAK_SKILLS:
                continue
            variants = _skill_variants(normalized)
            key = min(variants)
            occurrences[key] += 1
            display_by_key.setdefault(key, raw_skill.strip())
            variants_by_key[key] = variants
            if all(item.evidence_id != evidence.evidence_id for item in evidence_by_key[key]):
                evidence_by_key[key].append(evidence)

    ordered_keys = sorted(
        occurrences,
        key=lambda key: (
            -occurrences[key],
            min(item.source_order for item in evidence_by_key[key]),
            display_by_key[key].casefold(),
        ),
    )
    skills = tuple(
        _ProfileSkill(
            display=display_by_key[key],
            variants=variants_by_key[key],
            evidence=tuple(sorted(evidence_by_key[key], key=lambda item: item.source_order)),
        )
        for key in ordered_keys
    )
    profile_text = " ".join(
        (
            profile.summary or "",
            *(f"{item.title} {item.content}" for item in profile.evidence),
        )
    )
    expansions = (
        _expansion_queries(query, skills, profile_text=profile_text) if smart_expand else ()
    )
    snapshot = SearchProfileSnapshot(
        candidate_id=profile.candidate_id,
        profile_version=profile.profile_version,
        evidence_count=len(profile.evidence),
        skill_count=len(skills),
        skills=tuple(item.display for item in skills[:20]),
        expansion_queries=expansions,
    )
    education_text = _normalize(
        " ".join(
            f"{item.title} {item.content}"
            for item in profile.evidence
            if item.evidence_type is EvidenceType.EDUCATION
        )
    )
    return CandidateSearchProfile(
        snapshot=snapshot,
        skills=skills,
        evidence=profile.evidence,
        education_text=education_text,
    )


def rank_for_profile(
    job: DiscoveredJob,
    preference: JobSearchPreference,
    profile: CandidateSearchProfile,
) -> ProfileRankingResult:
    """Rank a job against explicit target intent and citable profile evidence."""
    title = _normalize(job.title)
    job_text = _normalize(
        " ".join(
            (
                job.title,
                job.description,
                " ".join(job.skills),
                job.experience,
                job.education,
            )
        )
    )
    query_terms = _terms(preference.query)
    title_query_matches = tuple(term for term in query_terms if _contains(title, term))
    body_query_matches = tuple(term for term in query_terms if _contains(job_text, term))
    target_score = _target_score(
        title=title,
        normalized_query=_normalize(preference.query),
        query_terms=query_terms,
        title_matches=title_query_matches,
        body_matches=body_query_matches,
    )

    matched_skills = tuple(
        skill
        for skill in profile.skills
        if any(_contains(job_text, item) for item in skill.variants)
    )
    skill_score = min(30, sum(_skill_weight(item.display) for item in matched_skills))
    matched_evidence = _matched_evidence(
        profile,
        matched_skills,
        job_text=job_text,
        job_education=job.education,
    )
    evidence_score = min(
        10,
        sum(_evidence_weight(item, profile.evidence) for item in matched_evidence),
    )
    preference_score = _preference_score(job, preference)
    information_score = _information_score(job)
    total = target_score + skill_score + evidence_score + preference_score + information_score
    breakdown = DiscoveryRankBreakdown(
        target_relevance=target_score,
        profile_skills=skill_score,
        profile_evidence=evidence_score,
        preference_fit=preference_score,
        information_quality=information_score,
        total=total,
    )
    return ProfileRankingResult(
        breakdown=breakdown,
        matched_query_terms=tuple(sorted(set(body_query_matches))),
        matched_profile_skills=tuple(item.display for item in matched_skills),
        matched_evidence=matched_evidence,
    )


def _target_score(
    *,
    title: str,
    normalized_query: str,
    query_terms: tuple[str, ...],
    title_matches: tuple[str, ...],
    body_matches: tuple[str, ...],
) -> int:
    if normalized_query and _contains(title, normalized_query):
        return 40
    if not query_terms:
        return 0
    title_ratio = len(title_matches) / len(query_terms)
    body_ratio = len(body_matches) / len(query_terms)
    return min(40, round(32 * title_ratio + 8 * body_ratio))


def _matched_evidence(
    profile: CandidateSearchProfile,
    skills: tuple[_ProfileSkill, ...],
    *,
    job_text: str,
    job_education: str,
) -> tuple[ProfileEvidenceMatch, ...]:
    matched_by_evidence: defaultdict[str, list[str]] = defaultdict(list)
    evidence_by_id = {item.evidence_id: item for item in profile.evidence}
    for skill in skills:
        for evidence in skill.evidence:
            matched_by_evidence[evidence.evidence_id].append(skill.display)
    normalized_education = _normalize(job_education)
    for level in ("博士", "硕士", "本科", "大专", "高中"):
        if level not in normalized_education or level not in profile.education_text:
            continue
        for evidence in profile.evidence:
            if evidence.evidence_type is EvidenceType.EDUCATION and level in _normalize(
                f"{evidence.title} {evidence.content}"
            ):
                matched_by_evidence[evidence.evidence_id].append(f"学历: {level}")

    job_terms = set(_terms(job_text))
    context_candidates: list[tuple[CandidateEvidence, tuple[str, ...]]] = []
    for evidence in profile.evidence:
        if evidence.evidence_id in matched_by_evidence:
            continue
        evidence_terms = set(_terms(f"{evidence.title} {evidence.content}"))
        overlap = tuple(sorted(job_terms & evidence_terms, key=lambda value: (-len(value), value)))
        if overlap:
            context_candidates.append((evidence, overlap[:2]))
    context_candidates.sort(
        key=lambda item: (
            0 if item[0].evidence_type in {EvidenceType.EXPERIENCE, EvidenceType.PROJECT} else 1,
            item[0].source_order,
            item[0].evidence_id,
        )
    )
    for evidence, terms in context_candidates[:2]:
        matched_by_evidence[evidence.evidence_id].extend(terms)

    ordered = sorted(
        (evidence_by_id[evidence_id] for evidence_id in matched_by_evidence),
        key=lambda item: (item.source_order, item.evidence_id),
    )
    return tuple(
        ProfileEvidenceMatch(
            evidence_id=item.evidence_id,
            title=item.title,
            matched_terms=tuple(dict.fromkeys(matched_by_evidence[item.evidence_id])),
        )
        for item in ordered
    )


def _evidence_weight(
    match: ProfileEvidenceMatch,
    evidence: tuple[CandidateEvidence, ...],
) -> int:
    evidence_type = next(
        item.evidence_type for item in evidence if item.evidence_id == match.evidence_id
    )
    base = 3 if evidence_type in {EvidenceType.EXPERIENCE, EvidenceType.PROJECT} else 2
    return min(4, base + (1 if len(match.matched_terms) >= 2 else 0))


def _preference_score(job: DiscoveredJob, preference: JobSearchPreference) -> int:
    score = 0
    if preference.city and preference.city in job.location:
        score += 3
    if preference.employment_types and job.employment_type in preference.employment_types:
        score += 3
    if preference.company_sizes and job.company_size in preference.company_sizes:
        score += 2
    monthly_salary_matches = (
        preference.salary_min_k is not None or preference.salary_max_k is not None
    ) and job.salary_min_k is not None
    daily_salary_matches = (
        preference.daily_salary_min_yuan is not None or preference.daily_salary_max_yuan is not None
    ) and job.salary_daily_min_yuan is not None
    if monthly_salary_matches or daily_salary_matches:
        score += 2
    elif job.salary_min_k is not None or job.salary_daily_min_yuan is not None:
        score += 1
    return min(10, score)


def _information_score(job: DiscoveredJob) -> int:
    score = 0
    if len(job.description) >= 80:
        score += 4
    elif len(job.description) >= 30:
        score += 3
    elif job.description:
        score += 1
    score += int(bool(job.experience))
    score += int(bool(job.education))
    score += int(bool(job.salary_text))
    score += int(bool(job.published_text))
    if job.detail_fetched_at is not None:
        score += 2
    return min(10, score)


def _skill_weight(display: str) -> int:
    normalized = _normalize(display)
    if normalized in {"ai", "ml", "llm", "rag", "go", "c#", "c++"}:
        return 5
    return 6 if len(normalized) >= 3 else 5


def _skill_variants(normalized_skill: str) -> frozenset[str]:
    for group in _NORMALIZED_ALIAS_GROUPS:
        if normalized_skill in group:
            return group
    return frozenset((normalized_skill,))


def _expansion_queries(
    query: str,
    skills: tuple[_ProfileSkill, ...],
    *,
    profile_text: str,
) -> tuple[str, ...]:
    normalized_query = _normalize(query)
    normalized_profile = _normalize(profile_text)
    candidates: list[str] = []
    for markers, expansions in _ROLE_EXPANSIONS:
        if any(_normalize(marker) in normalized_query for marker in markers):
            candidates.extend(expansions)
            break
    if not candidates:
        for markers, expansions in _ROLE_EXPANSIONS:
            if any(_normalize(marker) in normalized_profile for marker in markers):
                candidates.append(expansions[0])
                break
    if skills:
        top_skill = skills[0].display
        if _normalize(top_skill) not in normalized_query:
            candidates.append(f"{query} {top_skill}")
    return tuple(
        value for value in dict.fromkeys(candidates) if _normalize(value) != normalized_query
    )[:2]


def _terms(value: str) -> tuple[str, ...]:
    normalized = _normalize(value)
    terms = set(_TOKEN_PATTERN.findall(normalized))
    for chunk in tuple(terms):
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", chunk):
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tuple(sorted(term for term in terms if term not in _CONTEXT_STOP_WORDS))


def _contains(text: str, term: str) -> bool:
    if not term:
        return False
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
