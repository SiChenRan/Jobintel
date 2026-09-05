"""JobIntel persistence fixtures backed by isolated test databases."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database


@pytest.fixture
def jobintel_db() -> Iterator[JobIntelDatabase]:
    database = JobIntelDatabase.connect(":memory:")
    seed_database(database)
    yield database
    database.close()


@pytest.fixture
def jobintel_repo(jobintel_db: JobIntelDatabase) -> SQLiteJobRepository:
    return SQLiteJobRepository(jobintel_db)
