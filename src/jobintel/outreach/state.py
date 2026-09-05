"""Pure state transitions for reviewed outreach revisions."""

from __future__ import annotations

from jobintel.outreach.models import OutreachEventType, OutreachStatus


class OutreachStateTransitionError(ValueError):
    """Raised when an outreach revision receives an invalid lifecycle action."""


_STATUS_TRANSITIONS: dict[OutreachStatus, frozenset[OutreachStatus]] = {
    OutreachStatus.DRAFT: frozenset({OutreachStatus.APPROVED, OutreachStatus.DISMISSED}),
    OutreachStatus.APPROVED: frozenset({OutreachStatus.SENT_CONFIRMED, OutreachStatus.DISMISSED}),
    OutreachStatus.SENT_CONFIRMED: frozenset(),
    OutreachStatus.DISMISSED: frozenset(),
}


def transition_outreach_status(current: OutreachStatus, target: OutreachStatus) -> OutreachStatus:
    """Validate and return one explicit outreach status transition."""
    if target not in _STATUS_TRANSITIONS[current]:
        raise OutreachStateTransitionError(
            f"invalid outreach status transition: {current.value} -> {target.value}"
        )
    return target


def validate_outreach_event(status: OutreachStatus, event: OutreachEventType) -> None:
    """Reject events that are not meaningful for the current review state."""
    if event is OutreachEventType.APPROVED:
        transition_outreach_status(status, OutreachStatus.APPROVED)
        return
    if event is OutreachEventType.DISMISSED:
        transition_outreach_status(status, OutreachStatus.DISMISSED)
        return
    if event is OutreachEventType.SENT_CONFIRMED:
        transition_outreach_status(status, OutreachStatus.SENT_CONFIRMED)
        return
    if event in (OutreachEventType.COPIED, OutreachEventType.OPENED):
        if status is not OutreachStatus.APPROVED:
            raise OutreachStateTransitionError(
                f"{event.value} event requires an approved outreach revision"
            )
        return
    raise OutreachStateTransitionError(f"unknown outreach event: {event}")
