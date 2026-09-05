"""Pure deterministic hard-requirement scoring policy for JobIntel V2."""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

from jobintel.models import (
    JobRequirement,
    MatchStatus,
    Recommendation,
    RequirementCategory,
    RequirementImportance,
    RequirementMatchDraft,
    ScoreBreakdown,
    ScoreGroupBreakdown,
)

SCORING_VERSION = "jobintel-scoring-v2-hard-requirements"

_QUANTIFIED_EXPERIENCE = re.compile(
    r"(?:\d+(?:\.\d+)?|一|二|两|三|四|五|六|七|八|九|十|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?:年|个月|月|years?|months?)",
    re.IGNORECASE,
)
_NON_PROFESSIONAL_SKILL_MARKERS = (
    "沟通",
    "协作",
    "责任心",
    "抗压",
    "自驱",
    "学习能力",
    "执行力",
    "用户需求",
    "产品模块",
    "产品功能",
    "团队合作",
    "communication",
    "collaboration",
    "ownership",
    "user need",
    "product vision",
)
_BONUS_MARKERS = ("加分项", "加分", "bonus", "plus")
_PREFERRED_MARKERS = (
    "优先",
    "更佳",
    "可选",
    "preferred",
    "nice to have",
    "optional",
)

STATUS_VALUES = MappingProxyType(
    {
        MatchStatus.MATCHED: Decimal("1.0"),
        MatchStatus.PARTIAL: Decimal("0.5"),
        MatchStatus.MISSING: Decimal("0.0"),
    }
)

IMPORTANCE_WEIGHTS = MappingProxyType(
    {
        RequirementImportance.MUST: Decimal("0.60"),
        RequirementImportance.PREFERRED: Decimal("0.25"),
        RequirementImportance.BONUS: Decimal("0.15"),
    }
)


class ScoringError(ValueError):
    """Raised when requirements and matches cannot form a complete score."""


def is_scoreable_requirement(requirement: JobRequirement) -> bool:
    """Return whether a requirement is a concrete, auditable hiring threshold."""
    skill = (requirement.normalized_skill or "").casefold()
    if skill and any(marker in skill for marker in _NON_PROFESSIONAL_SKILL_MARKERS):
        return False
    if requirement.category is RequirementCategory.SKILL:
        return bool(skill)
    if requirement.category in (
        RequirementCategory.EDUCATION,
        RequirementCategory.LANGUAGE,
    ):
        return True
    if requirement.category in (
        RequirementCategory.EXPERIENCE,
        RequirementCategory.PROJECT,
    ):
        return bool(skill) or bool(_QUANTIFIED_EXPERIENCE.search(requirement.text))
    return False


def scoring_importance(requirement: JobRequirement) -> RequirementImportance:
    """Resolve program-controlled importance for named professional skills."""
    if requirement.category is not RequirementCategory.SKILL:
        return requirement.importance
    text = requirement.text.casefold()
    if any(marker in text for marker in _BONUS_MARKERS):
        return RequirementImportance.BONUS
    if any(marker in text for marker in _PREFERRED_MARKERS):
        return RequirementImportance.PREFERRED
    return RequirementImportance.MUST


def _index_inputs(
    requirements: Sequence[JobRequirement], matches: Sequence[RequirementMatchDraft]
) -> tuple[dict[str, JobRequirement], dict[str, RequirementMatchDraft]]:
    """Index inputs and reject duplicate or non-corresponding identities."""
    if not requirements:
        raise ScoringError("cannot score a job without requirements")

    requirement_by_id: dict[str, JobRequirement] = {}
    for requirement in requirements:
        if requirement.requirement_id in requirement_by_id:
            raise ScoringError(f"duplicate requirement_id: {requirement.requirement_id}")
        requirement_by_id[requirement.requirement_id] = requirement

    match_by_id: dict[str, RequirementMatchDraft] = {}
    for match in matches:
        if match.requirement_id in match_by_id:
            raise ScoringError(f"duplicate match for requirement_id: {match.requirement_id}")
        match_by_id[match.requirement_id] = match

    missing = sorted(requirement_by_id.keys() - match_by_id.keys())
    unknown = sorted(match_by_id.keys() - requirement_by_id.keys())
    if missing:
        raise ScoringError(f"missing matches for requirement_ids: {', '.join(missing)}")
    if unknown:
        raise ScoringError(f"unknown matched requirement_ids: {', '.join(unknown)}")
    return requirement_by_id, match_by_id


def score_requirements(
    requirements: Sequence[JobRequirement], matches: Sequence[RequirementMatchDraft]
) -> ScoreBreakdown:
    """Calculate the V2 hard-requirement-only 0--100 fit score.

    Importance weights are renormalized over groups present in the job. Decimal
    arithmetic is retained through the entire calculation and the final score
    is rounded exactly once with ``ROUND_HALF_UP``.

    Args:
        requirements: All requirements; qualitative context is audited but excluded.
        matches: Exactly one validated match for every requirement.

    Returns:
        The rounded score and an auditable per-group breakdown.

    Raises:
        ScoringError: If inputs are empty, duplicated, missing, or unknown.
    """
    all_requirements, match_by_id = _index_inputs(requirements, matches)
    requirement_by_id = {
        requirement_id: requirement
        for requirement_id, requirement in all_requirements.items()
        if is_scoreable_requirement(requirement)
    }
    if not requirement_by_id:
        raise ScoringError("cannot score a job without concrete hard requirements")
    populated_groups = {
        scoring_importance(requirement) for requirement in requirement_by_id.values()
    }
    populated_weight = sum(
        (IMPORTANCE_WEIGHTS[importance] for importance in populated_groups),
        start=Decimal("0"),
    )

    groups: list[ScoreGroupBreakdown] = []
    contribution_total = Decimal("0")
    for importance in RequirementImportance:
        group_requirements = [
            requirement
            for requirement in requirement_by_id.values()
            if scoring_importance(requirement) is importance
        ]
        if not group_requirements:
            continue

        status_total = sum(
            (
                STATUS_VALUES[match_by_id[requirement.requirement_id].status]
                for requirement in group_requirements
            ),
            start=Decimal("0"),
        )
        status_mean = status_total / Decimal(len(group_requirements))
        base_weight = IMPORTANCE_WEIGHTS[importance]
        normalized_weight = base_weight / populated_weight
        weighted_contribution = normalized_weight * status_mean
        contribution_total += weighted_contribution
        groups.append(
            ScoreGroupBreakdown(
                importance=importance,
                requirement_count=len(group_requirements),
                status_total=status_total,
                status_mean=status_mean,
                base_weight=base_weight,
                normalized_weight=normalized_weight,
                weighted_contribution=weighted_contribution,
            )
        )

    raw_score = contribution_total * Decimal("100")
    rounded_score = int(raw_score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    score = min(100, max(0, rounded_score))
    return ScoreBreakdown(
        groups=tuple(groups),
        scored_requirement_ids=tuple(requirement_by_id),
        excluded_requirement_ids=tuple(
            requirement_id
            for requirement_id in all_requirements
            if requirement_id not in requirement_by_id
        ),
        raw_score=raw_score,
        score=score,
    )


def derive_recommendation(score: int) -> Recommendation:
    """Map a program score to the V1 recommendation thresholds.

    Args:
        score: Deterministic integer score in the inclusive 0--100 range.

    Returns:
        The corresponding recommendation enum.

    Raises:
        ValueError: If score is outside the supported range.
    """
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("score must be an integer from 0 to 100")
    if score >= 80:
        return Recommendation.STRONG_APPLY
    if score >= 65:
        return Recommendation.APPLY
    if score >= 45:
        return Recommendation.LOW_PRIORITY
    return Recommendation.SKIP
