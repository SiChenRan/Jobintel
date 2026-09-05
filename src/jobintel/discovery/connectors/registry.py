"""Composition root for maintained discovery connectors."""

from __future__ import annotations

from jobintel.discovery.connectors.base import JobSourceConnector
from jobintel.discovery.connectors.boss import BossConnector
from jobintel.discovery.models import JobSource


def build_connectors(
    *,
    cdp_port: int = 9222,
    search_min_delay_seconds: float = 1.2,
    search_max_delay_seconds: float = 2.4,
    detail_min_delay_seconds: float = 3.0,
    detail_max_delay_seconds: float = 6.0,
) -> dict[JobSource, JobSourceConnector]:
    """Build one connector per maintained source."""
    return {
        JobSource.BOSS: BossConnector(
            cdp_port=cdp_port,
            search_min_delay_seconds=search_min_delay_seconds,
            search_max_delay_seconds=search_max_delay_seconds,
            detail_min_delay_seconds=detail_min_delay_seconds,
            detail_max_delay_seconds=detail_max_delay_seconds,
        )
    }
