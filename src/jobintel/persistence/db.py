"""SQLite connection and transaction ownership for JobIntel."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self


class JobIntelDatabase:
    """Own one configured SQLite connection.

    Repository leaf operations never commit. Callers use :meth:`transaction`
    to define aggregate boundaries for seeding and analysis persistence.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Wrap a configured SQLite connection."""
        self.connection = connection

    @classmethod
    def connect(cls, db_path: Path | str = ":memory:") -> Self:
        """Open a JobIntel database at a file path or in memory."""
        resolved_path: str
        if db_path == ":memory:":
            resolved_path = ":memory:"
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path = str(path)

        connection = sqlite3.connect(resolved_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return cls(connection)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Open one non-nested transaction and commit or roll it back atomically."""
        if self.connection.in_transaction:
            raise RuntimeError("nested JobIntel database transactions are not supported")
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """Close the owned SQLite connection."""
        self.connection.close()

    def __enter__(self) -> Self:
        """Return this database for context-managed ownership."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the database when leaving an ownership context."""
        self.close()
