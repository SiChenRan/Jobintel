"""Pure deterministic scoring policy for JobIntel V1."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType

from jobintel.models import (
    JobRequirement,
    MatchStatus,
    Recommendation,
    RequirementImportance,
    RequirementMatchDraft,
    ScoreBreakdown,
    ScoreGroupBreakdown,
)

SCORING_VERSION = "jobintel-scoring-v1"

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
    """Calculate the V1 0--100 fit score.

    Importance weights are renormalized over groups present in the job. Decimal
    arithmetic is retained through the entire calculation and the final score
    is rounded exactly once with ``ROUND_HALF_UP``.

    Args:
        requirements: The complete requirements for one immutable job version.
        matches: Exactly one validated match for every requirement.

    Returns:
        The rounded score and an auditable per-group breakdown.

    Raises:
        ScoringError: If inputs are empty, duplicated, missing, or unknown.
    """
    requirement_by_id, match_by_id = _index_inputs(requirements, matches)
    populated_groups = {requirement.importance for requirement in requirement_by_id.values()}
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
            if requirement.importance is importance
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
    return ScoreBreakdown(groups=tuple(groups), raw_score=raw_score, score=score)


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
