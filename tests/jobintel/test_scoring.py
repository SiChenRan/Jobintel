"""Tests for deterministic JobIntel scoring and recommendation thresholds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from jobintel.models import (
    JobRequirement,
    MatchStatus,
    Recommendation,
    RequirementCategory,
    RequirementImportance,
    RequirementMatchDraft,
)
from jobintel.scoring import ScoringError, derive_recommendation, score_requirements


def _requirement(identifier: str, importance: RequirementImportance) -> JobRequirement:
    return JobRequirement(
        requirement_id=identifier,
        text=f"Requirement {identifier}",
        category=RequirementCategory.SKILL,
        importance=importance,
        source_order=int(identifier.removeprefix("r")),
    )


def _match(identifier: str, status: MatchStatus) -> RequirementMatchDraft:
    evidence_ids = () if status is MatchStatus.MISSING else (f"ev-{identifier}",)
    return RequirementMatchDraft(
        requirement_id=identifier,
        status=status,
        evidence_ids=evidence_ids,
        confidence=0.5,
        reason="Deterministic fixture",
    )


def test_only_populated_importance_groups_are_normalized() -> None:
    requirements = [
        _requirement("r0", RequirementImportance.MUST),
        _requirement("r1", RequirementImportance.PREFERRED),
    ]
    matches = [
        _match("r0", MatchStatus.MATCHED),
        _match("r1", MatchStatus.MISSING),
    ]

    result = score_requirements(requirements, matches)

    assert result.score == 71
    assert [group.importance for group in result.groups] == [
        RequirementImportance.MUST,
        RequirementImportance.PREFERRED,
    ]
    assert result.groups[0].normalized_weight == Decimal("0.60") / Decimal("0.85")
    assert result.groups[1].normalized_weight == Decimal("0.25") / Decimal("0.85")
    assert sum(group.normalized_weight for group in result.groups) == Decimal("1")


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((MatchStatus.MATCHED,), 100),
        ((MatchStatus.PARTIAL,), 50),
        ((MatchStatus.MISSING,), 0),
    ],
)
def test_status_values_drive_score(statuses: tuple[MatchStatus, ...], expected: int) -> None:
    requirements = [_requirement("r0", RequirementImportance.MUST)]
    matches = [_match("r0", statuses[0])]
    assert score_requirements(requirements, matches).score == expected


def test_round_half_up_occurs_once_after_full_calculation() -> None:
    requirements = [_requirement(f"r{index}", RequirementImportance.MUST) for index in range(4)]
    statuses = (
        MatchStatus.MATCHED,
        MatchStatus.MATCHED,
        MatchStatus.PARTIAL,
        MatchStatus.MISSING,
    )
    matches = [_match(f"r{index}", status) for index, status in enumerate(statuses)]

    result = score_requirements(requirements, matches)

    assert result.raw_score == Decimal("62.500")
    assert result.score == 63


def test_all_three_importance_groups_use_declared_weights() -> None:
    requirements = [
        _requirement("r0", RequirementImportance.MUST),
        _requirement("r1", RequirementImportance.PREFERRED),
        _requirement("r2", RequirementImportance.BONUS),
    ]
    matches = [
        _match("r0", MatchStatus.MATCHED),
        _match("r1", MatchStatus.PARTIAL),
        _match("r2", MatchStatus.MISSING),
    ]

    result = score_requirements(requirements, matches)

    assert result.raw_score == Decimal("72.500")
    assert result.score == 73


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, Recommendation.SKIP),
        (44, Recommendation.SKIP),
        (45, Recommendation.LOW_PRIORITY),
        (64, Recommendation.LOW_PRIORITY),
        (65, Recommendation.APPLY),
        (79, Recommendation.APPLY),
        (80, Recommendation.STRONG_APPLY),
        (100, Recommendation.STRONG_APPLY),
    ],
)
def test_recommendation_boundaries(score: int, expected: Recommendation) -> None:
    assert derive_recommendation(score) is expected


@pytest.mark.parametrize("score", [-1, 101, True, 45.5])
def test_recommendation_rejects_non_integer_or_out_of_range_scores(score: object) -> None:
    with pytest.raises(ValueError, match="integer from 0 to 100"):
        derive_recommendation(score)  # type: ignore[arg-type]


def test_scoring_rejects_empty_incomplete_unknown_and_duplicate_inputs() -> None:
    requirement = _requirement("r0", RequirementImportance.MUST)
    match = _match("r0", MatchStatus.MATCHED)

    with pytest.raises(ScoringError, match="without requirements"):
        score_requirements([], [])
    with pytest.raises(ScoringError, match="missing matches"):
        score_requirements([requirement], [])
    with pytest.raises(ScoringError, match="unknown matched"):
        score_requirements([requirement], [match, _match("r1", MatchStatus.MATCHED)])
    with pytest.raises(ScoringError, match="duplicate requirement_id"):
        score_requirements([requirement, requirement], [match])
    with pytest.raises(ScoringError, match="duplicate match"):
        score_requirements([requirement], [match, match])
