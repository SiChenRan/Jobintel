"""Run-scoped provenance observations and evidence receipts.

The ledger stores IDs, scopes, hashes, counts, and timing only. Full job,
profile, evidence, and transcript content never enters provenance telemetry.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from jobintel.models import (
    CandidateEvidence,
    FrozenDomainModel,
    NonEmptyStr,
    SearchCandidateEvidenceOutput,
    Sha256Hex,
)

PROVENANCE_VERSION = "jobintel-provenance-v1"


def canonical_sha256(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    """Hash JSON-compatible tool input or output without retaining its content."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evidence_content_sha256(evidence: CandidateEvidence) -> str:
    """Hash exact evidence content for later profile-integrity validation."""
    return hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()


class EntityKind(StrEnum):
    """Kinds of versioned entities that a tool may return."""

    JOB = "job"
    CANDIDATE_PROFILE = "candidate_profile"
    COMPANY = "company"
    EVIDENCE = "evidence"


class EntityRef(FrozenDomainModel):
    """Content-free reference to an entity returned by a tool."""

    kind: EntityKind
    entity_id: NonEmptyStr
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_versioned_kind(self) -> Self:
        """Require versions for immutable Job and Candidate Profile references."""
        if self.kind in (EntityKind.JOB, EntityKind.CANDIDATE_PROFILE) and self.version is None:
            raise ValueError(f"{self.kind.value} entity reference requires a version")
        return self


class EvidenceSearchScope(FrozenDomainModel):
    """Complete scope of one candidate evidence search call."""

    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    requirement_id: NonEmptyStr
    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)


class EvidenceReceipt(FrozenDomainModel):
    """Proof that one scoped search returned one exact evidence item."""

    candidate_id: NonEmptyStr
    profile_version: int = Field(ge=1)
    job_id: NonEmptyStr
    job_version: int = Field(ge=1)
    requirement_id: NonEmptyStr
    evidence_id: NonEmptyStr
    tool_call_id: NonEmptyStr
    content_sha256: Sha256Hex

    @property
    def scope(self) -> EvidenceSearchScope:
        """Project this receipt to its search scope."""
        return EvidenceSearchScope(
            job_id=self.job_id,
            job_version=self.job_version,
            requirement_id=self.requirement_id,
            candidate_id=self.candidate_id,
            profile_version=self.profile_version,
        )


class ToolObservation(FrozenDomainModel):
    """Content-free telemetry for one dispatched tool call."""

    tool_call_id: NonEmptyStr
    tool_name: NonEmptyStr
    input_sha256: Sha256Hex
    output_sha256: Sha256Hex
    success: bool
    error_code: NonEmptyStr | None = None
    iteration: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    returned_entity_refs: tuple[EntityRef, ...] = ()
    evidence_search_scope: EvidenceSearchScope | None = None

    @model_validator(mode="after")
    def validate_error_state(self) -> Self:
        """Keep success and structured error state mutually consistent."""
        if self.success and self.error_code is not None:
            raise ValueError("successful tool observation must not contain error_code")
        if not self.success and self.error_code is None:
            raise ValueError("failed tool observation requires error_code")
        return self


class ProvenanceSnapshot(FrozenDomainModel):
    """Immutable export of a run ledger for persistence or evaluation."""

    run_id: NonEmptyStr
    version: NonEmptyStr = PROVENANCE_VERSION
    observations: tuple[ToolObservation, ...]
    evidence_receipts: tuple[EvidenceReceipt, ...]
    digest: Sha256Hex


class ProvenanceLedger:
    """Mutable, run-local ledger written only by the future tool dispatcher."""

    def __init__(self, run_id: str) -> None:
        """Create an empty ledger for one agent run."""
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        self.run_id = run_id.strip()
        self._observations: dict[str, ToolObservation] = {}
        self._receipts: list[EvidenceReceipt] = []

    @property
    def observations(self) -> tuple[ToolObservation, ...]:
        """Return observations in dispatcher insertion order."""
        return tuple(self._observations.values())

    @property
    def evidence_receipts(self) -> tuple[EvidenceReceipt, ...]:
        """Return receipts in dispatcher insertion order."""
        return tuple(self._receipts)

    def has_tool_call(self, tool_call_id: str) -> bool:
        """Return whether the dispatcher already recorded this call identity."""
        return tool_call_id in self._observations

    def record_observation(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        tool_output: BaseModel | dict[str, Any] | list[Any],
        success: bool,
        iteration: int,
        duration_ms: int,
        error_code: str | None = None,
        returned_entity_refs: tuple[EntityRef, ...] = (),
        evidence_search_scope: EvidenceSearchScope | None = None,
    ) -> ToolObservation:
        """Record one generic tool result after dispatcher execution."""
        observation = ToolObservation(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            input_sha256=canonical_sha256(tool_input),
            output_sha256=canonical_sha256(tool_output),
            success=success,
            error_code=error_code,
            iteration=iteration,
            duration_ms=duration_ms,
            returned_entity_refs=returned_entity_refs,
            evidence_search_scope=evidence_search_scope,
        )
        self._append(observation, ())
        return observation

    def record_evidence_search(
        self,
        *,
        tool_call_id: str,
        tool_input: BaseModel | dict[str, Any],
        output: SearchCandidateEvidenceOutput,
        iteration: int,
        duration_ms: int,
    ) -> tuple[ToolObservation, tuple[EvidenceReceipt, ...]]:
        """Atomically record a successful scoped search and all returned evidence."""
        scope = EvidenceSearchScope(
            job_id=output.job_id,
            job_version=output.job_version,
            requirement_id=output.requirement_id,
            candidate_id=output.candidate_id,
            profile_version=output.profile_version,
        )
        receipts = tuple(
            EvidenceReceipt(
                candidate_id=scope.candidate_id,
                profile_version=scope.profile_version,
                job_id=scope.job_id,
                job_version=scope.job_version,
                requirement_id=scope.requirement_id,
                evidence_id=hit.evidence.evidence_id,
                tool_call_id=tool_call_id,
                content_sha256=evidence_content_sha256(hit.evidence),
            )
            for hit in output.hits
        )
        observation = ToolObservation(
            tool_call_id=tool_call_id,
            tool_name="search_candidate_evidence",
            input_sha256=canonical_sha256(tool_input),
            output_sha256=canonical_sha256(output),
            success=True,
            iteration=iteration,
            duration_ms=duration_ms,
            returned_entity_refs=tuple(
                EntityRef(
                    kind=EntityKind.EVIDENCE,
                    entity_id=hit.evidence.evidence_id,
                    version=scope.profile_version,
                )
                for hit in output.hits
            ),
            evidence_search_scope=scope,
        )
        self._append(observation, receipts)
        return observation, receipts

    def _append(self, observation: ToolObservation, receipts: tuple[EvidenceReceipt, ...]) -> None:
        """Append one observation and receipts after enforcing call identity."""
        if observation.tool_call_id in self._observations:
            raise ValueError(f"duplicate tool_call_id: {observation.tool_call_id}")
        receipt_keys = {
            (receipt.tool_call_id, receipt.requirement_id, receipt.evidence_id)
            for receipt in self._receipts
        }
        new_keys = [
            (receipt.tool_call_id, receipt.requirement_id, receipt.evidence_id)
            for receipt in receipts
        ]
        if len(new_keys) != len(set(new_keys)) or any(key in receipt_keys for key in new_keys):
            raise ValueError("duplicate evidence receipt identity")
        self._observations[observation.tool_call_id] = observation
        self._receipts.extend(receipts)

    def has_entity(self, reference: EntityRef) -> bool:
        """Return whether a successful tool call returned an exact entity ref."""
        return any(
            observation.success and reference in observation.returned_entity_refs
            for observation in self._observations.values()
        )

    def has_successful_search(self, scope: EvidenceSearchScope) -> bool:
        """Return whether the exact scope had a successful evidence search."""
        return any(
            observation.success and observation.evidence_search_scope == scope
            for observation in self._observations.values()
        )

    def receipts_for_scope(self, scope: EvidenceSearchScope) -> tuple[EvidenceReceipt, ...]:
        """Return all evidence receipts issued for an exact search scope."""
        return tuple(receipt for receipt in self._receipts if receipt.scope == scope)

    def receipts_for_evidence(self, evidence_id: str) -> tuple[EvidenceReceipt, ...]:
        """Return receipts for an evidence ID across every scope in this run."""
        return tuple(receipt for receipt in self._receipts if receipt.evidence_id == evidence_id)

    def snapshot(self) -> ProvenanceSnapshot:
        """Build an immutable snapshot and digest without exposing source content."""
        payload = {
            "run_id": self.run_id,
            "version": PROVENANCE_VERSION,
            "observations": [item.model_dump(mode="json") for item in self.observations],
            "evidence_receipts": [item.model_dump(mode="json") for item in self.evidence_receipts],
        }
        return ProvenanceSnapshot(
            **payload,
            digest=canonical_sha256(payload),
        )
