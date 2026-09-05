"""Connector protocol and failure taxonomy for job discovery."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jobintel.discovery.models import (
    DetailFetchResult,
    JobSearchPreference,
    JobSource,
    JobSourceLink,
    RawJobListing,
)


class ConnectorError(RuntimeError):
    """Base error for a source that could not return a trustworthy result."""


class AuthenticationRequiredError(ConnectorError):
    """The source needs an interactive user login."""


class SourceBlockedError(ConnectorError):
    """The source returned a CAPTCHA, WAF, or risk-control response."""


class SourceUnavailableError(ConnectorError):
    """A local prerequisite or transport is unavailable."""


class JobSourceConnector(Protocol):
    """One bounded, replaceable third-party source adapter."""

    source: JobSource

    def search(self, preference: JobSearchPreference, *, limit: int) -> tuple[RawJobListing, ...]:
        """Return at most ``limit`` normalized live listings."""


@runtime_checkable
class JobDetailConnector(Protocol):
    """Optional source capability for serial, bounded detail enrichment."""

    source: JobSource

    def fetch_details(self, links: tuple[JobSourceLink, ...]) -> tuple[DetailFetchResult, ...]:
        """Fetch job details serially and stop immediately on source risk control."""
