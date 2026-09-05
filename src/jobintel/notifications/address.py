"""Validation helpers for notification mailbox addresses."""

from __future__ import annotations

from email.utils import parseaddr


def validate_email_address(value: str, *, label: str) -> str:
    """Reject empty, malformed, or header-injecting mailbox values."""
    normalized = value.strip()
    parsed = parseaddr(normalized)[1]
    if (
        not normalized
        or "\r" in normalized
        or "\n" in normalized
        or parsed != normalized
        or parsed.count("@") != 1
        or any(not part for part in parsed.rsplit("@", 1))
    ):
        raise ValueError(f"invalid {label} email address")
    return normalized


def mask_email_address(value: str) -> str:
    """Return a non-reversible display form suitable for audit records."""
    local, domain = validate_email_address(value, label="recipient").rsplit("@", 1)
    return f"{local[0]}***@{domain}"


__all__ = ["mask_email_address", "validate_email_address"]
