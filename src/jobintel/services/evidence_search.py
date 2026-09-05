"""Deterministic exact, alias, lexical, and fuzzy evidence retrieval."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from types import MappingProxyType

from jobintel.errors import EvidenceSearchError
from jobintel.models import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceMatchMethod,
    EvidenceSearchHit,
    JobPosting,
    JobRequirement,
    SearchCandidateEvidenceOutput,
)

_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[.+#/-][^\W_]+)*", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
        "with",
        "experience",
        "skills",
        "strong",
    }
)
_METHOD_PRIORITY = {
    EvidenceMatchMethod.EXACT: 4,
    EvidenceMatchMethod.ALIAS: 3,
    EvidenceMatchMethod.LEXICAL: 2,
    EvidenceMatchMethod.FUZZY: 1,
}

DEFAULT_EVIDENCE_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "amazon web services": ("aws",),
        "google cloud platform": ("gcp",),
        "kubernetes": ("k8s",),
        "machine learning": ("ml",),
        "postgresql": ("postgres", "postgres sql", "pg"),
        "pytorch": ("torch",),
        "rest api": ("restful api", "http api"),
    }
)


def normalize_search_text(value: str) -> str:
    """Normalize Unicode, casing, punctuation spacing, and whitespace."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _tokens(value: str) -> frozenset[str]:
    """Extract stable Unicode-aware search tokens and remove weak stop words."""
    return frozenset(
        token
        for token in _TOKEN_PATTERN.findall(normalize_search_text(value))
        if token not in _STOP_WORDS
    )


def _contains_phrase(text: str, phrase: str) -> bool:
    """Match a normalized phrase without matching inside a larger word."""
    pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
    return re.search(pattern, text) is not None


@dataclass(frozen=True)
class _RankedEvidence:
    evidence: CandidateEvidence
    method: EvidenceMatchMethod
    score: float
    matched_terms: tuple[str, ...]


class EvidenceSearchService:
    """Search only the supplied immutable candidate profile version."""

    def __init__(self, aliases: Mapping[str, tuple[str, ...]] = DEFAULT_EVIDENCE_ALIASES) -> None:
        """Normalize alias groups once for deterministic lookup."""
        groups = []
        for canonical, values in sorted(aliases.items()):
            normalized_group = frozenset(
                normalize_search_text(value) for value in (canonical, *values)
            )
            if "" in normalized_group:
                raise ValueError("evidence aliases must not be empty")
            groups.append(normalized_group)
        self._alias_groups = tuple(groups)

    def search(
        self,
        *,
        job: JobPosting,
        requirement: JobRequirement,
        profile: CandidateProfile,
        query: str,
        top_k: int = 5,
    ) -> SearchCandidateEvidenceOutput:
        """Return ranked evidence for one explicit Job/Requirement/Profile scope."""
        normalized_query = normalize_search_text(query)
        if not normalized_query:
            raise EvidenceSearchError("evidence search query must not be empty")
        if not 1 <= top_k <= 20:
            raise EvidenceSearchError("top_k must be between 1 and 20")
        if requirement.requirement_id not in {item.requirement_id for item in job.requirements}:
            raise EvidenceSearchError(
                "requirement does not belong to the supplied job version: "
                f"{requirement.requirement_id}"
            )

        query_phrases = {normalized_query}
        if requirement.normalized_skill is not None:
            query_phrases.add(normalize_search_text(requirement.normalized_skill))
        query_tokens = _tokens(f"{query} {requirement.text}")

        ranked = [
            result
            for evidence in profile.evidence
            if (result := self._rank(evidence, query_phrases, query_tokens)) is not None
        ]
        ranked.sort(
            key=lambda item: (
                -_METHOD_PRIORITY[item.method],
                -item.score,
                item.evidence.source_order,
                item.evidence.evidence_id,
            )
        )
        hits = tuple(
            EvidenceSearchHit(
                evidence=item.evidence,
                match_method=item.method,
                relevance_score=item.score,
                matched_terms=item.matched_terms,
            )
            for item in ranked[:top_k]
        )
        return SearchCandidateEvidenceOutput(
            job_id=job.job_id,
            job_version=job.job_version,
            requirement_id=requirement.requirement_id,
            candidate_id=profile.candidate_id,
            profile_version=profile.profile_version,
            query=query,
            hits=hits,
        )

    def _rank(
        self,
        evidence: CandidateEvidence,
        query_phrases: set[str],
        query_tokens: frozenset[str],
    ) -> _RankedEvidence | None:
        """Assign the highest-priority applicable deterministic match strategy."""
        searchable_fields = tuple(
            normalize_search_text(value)
            for value in (evidence.title, evidence.content, *evidence.skills)
        )
        exact_terms = tuple(
            sorted(
                phrase
                for phrase in query_phrases
                if any(_contains_phrase(field, phrase) for field in searchable_fields)
            )
        )
        if exact_terms:
            return _RankedEvidence(evidence, EvidenceMatchMethod.EXACT, 1.0, exact_terms)

        evidence_text = " ".join(searchable_fields)
        alias_terms = self._alias_matches(query_phrases, evidence_text)
        if alias_terms:
            return _RankedEvidence(evidence, EvidenceMatchMethod.ALIAS, 0.9, alias_terms)

        evidence_tokens = _tokens(evidence_text)
        lexical_terms = tuple(sorted(query_tokens & evidence_tokens))
        if lexical_terms:
            score = len(lexical_terms) / max(1, len(query_tokens))
            return _RankedEvidence(
                evidence,
                EvidenceMatchMethod.LEXICAL,
                round(min(0.89, score), 6),
                lexical_terms,
            )

        fuzzy_pairs = [
            (
                query_token,
                evidence_token,
                SequenceMatcher(None, query_token, evidence_token).ratio(),
            )
            for query_token in sorted(query_tokens)
            for evidence_token in sorted(evidence_tokens)
        ]
        if not fuzzy_pairs:
            return None
        best_query, _best_evidence, ratio = max(
            fuzzy_pairs, key=lambda item: (item[2], item[0], item[1])
        )
        if ratio < 0.78:
            return None
        return _RankedEvidence(
            evidence,
            EvidenceMatchMethod.FUZZY,
            round(min(0.79, ratio * 0.8), 6),
            (best_query,),
        )

    def _alias_matches(self, query_phrases: set[str], evidence_text: str) -> tuple[str, ...]:
        """Return canonical alias concepts connecting query and evidence."""
        matches = []
        query_text = " ".join(sorted(query_phrases))
        for group in self._alias_groups:
            query_variants = {variant for variant in group if _contains_phrase(query_text, variant)}
            evidence_variants = {
                variant for variant in group if _contains_phrase(evidence_text, variant)
            }
            if query_variants and evidence_variants and query_variants != evidence_variants:
                matches.append(sorted(group)[0])
        return tuple(sorted(matches))
