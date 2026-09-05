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
    modality = {
        RequirementImportance.MUST: "必须掌握",
        RequirementImportance.PREFERRED: "优先掌握",
        RequirementImportance.BONUS: "加分项",
    }[importance]
    return JobRequirement(
        requirement_id=identifier,
        text=f"{modality} Requirement {identifier}",
        category=RequirementCategory.SKILL,
        importance=importance,
        normalized_skill=identifier,
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


def test_qualitative_responsibilities_do_not_affect_hard_requirement_score() -> None:
    requirements = [
        _requirement("r0", RequirementImportance.MUST),
        _requirement("r1", RequirementImportance.MUST),
        JobRequirement(
            requirement_id="context-product",
            text="帮助把研发原型打磨成可以被真实用户使用的产品模块",
            category=RequirementCategory.PROJECT,
            importance=RequirementImportance.MUST,
            source_order=2,
        ),
        JobRequirement(
            requirement_id="context-needs",
            text="将真实用户需求转化为可开发、可验收的产品功能",
            category=RequirementCategory.OTHER,
            importance=RequirementImportance.MUST,
            source_order=3,
        ),
        JobRequirement(
            requirement_id="misclassified-product",
            text="推动用户需求成为可验收的产品功能",
            category=RequirementCategory.SKILL,
            importance=RequirementImportance.MUST,
            normalized_skill="产品功能",
            source_order=4,
        ),
    ]
    matches = [
        _match("r0", MatchStatus.MATCHED),
        _match("r1", MatchStatus.MISSING),
        _match("context-product", MatchStatus.MISSING),
        _match("context-needs", MatchStatus.MISSING),
        _match("misclassified-product", MatchStatus.MISSING),
    ]

    result = score_requirements(requirements, matches)

    assert result.score == 50
    assert result.scored_requirement_ids == ("r0", "r1")
    assert result.excluded_requirement_ids == (
        "context-product",
        "context-needs",
        "misclassified-product",
    )


def test_explicit_thresholds_and_named_project_technology_are_scoreable() -> None:
    requirements = (
        JobRequirement(
            requirement_id="years",
            text="至少 3 年后端开发经验",
            category=RequirementCategory.EXPERIENCE,
            importance=RequirementImportance.MUST,
            source_order=0,
        ),
        JobRequirement(
            requirement_id="rag-project",
            text="具备 RAG 项目经验",
            category=RequirementCategory.PROJECT,
            importance=RequirementImportance.PREFERRED,
            normalized_skill="RAG",
            source_order=1,
        ),
    )
    matches = tuple(_match(item.requirement_id, MatchStatus.MATCHED) for item in requirements)

    result = score_requirements(requirements, matches)

    assert result.score == 100
    assert result.scored_requirement_ids == ("years", "rag-project")


def test_named_technical_skill_defaults_to_must_unless_source_marks_preferred() -> None:
    requirements = (
        JobRequirement(
            requirement_id="python",
            text="会使用 Python",
            category=RequirementCategory.SKILL,
            importance=RequirementImportance.BONUS,
            normalized_skill="Python",
            source_order=0,
        ),
        JobRequirement(
            requirement_id="langchain",
            text="有 LangChain 经验者优先",
            category=RequirementCategory.SKILL,
            importance=RequirementImportance.MUST,
            normalized_skill="LangChain",
            source_order=1,
        ),
    )
    matches = (
        _match("python", MatchStatus.MATCHED),
        _match("langchain", MatchStatus.MISSING),
    )

    result = score_requirements(requirements, matches)

    assert [group.importance for group in result.groups] == [
        RequirementImportance.MUST,
        RequirementImportance.PREFERRED,
    ]
    assert result.score == 71


def test_scoring_rejects_jobs_with_only_qualitative_context() -> None:
    requirement = JobRequirement(
        requirement_id="context",
        text="与团队协作推动产品持续演进",
        category=RequirementCategory.OTHER,
        importance=RequirementImportance.MUST,
        source_order=0,
    )
    with pytest.raises(ScoringError, match="without concrete hard requirements"):
        score_requirements(
            (requirement,),
            (_match(requirement.requirement_id, MatchStatus.MISSING),),
        )


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
