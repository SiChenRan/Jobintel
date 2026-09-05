from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tests.conftest import FakeProvider

import jobintel.mcp_server.server as server_module
from jobintel.config import JobIntelSettings
from jobintel.persistence.db import JobIntelDatabase
from jobintel.providers.base import ToolCall, TurnResult
from jobintel.services.jd_parser import PARSER_SUBMIT_TOOL


async def test_default_server_seeds_lists_tools_and_closes_owned_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "mcp.db"
    database = JobIntelDatabase.connect(db_path)
    monkeypatch.setattr(
        server_module.JobIntelDatabase,
        "connect",
        lambda _path: database,
    )
    monkeypatch.setattr(
        server_module,
        "build_jobintel_provider",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("no key")),
    )

    server = server_module.build_default_server(
        JobIntelSettings(jobintel_db_path=db_path, anthropic_api_key=None)
    )
    async with Client(server) as client:
        tools = await client.list_tools()
        job = await client.call_tool("get_job", {"job_id": "J001"})
        with pytest.raises(ToolError, match="PARSER_NOT_AVAILABLE"):
            await client.call_tool("parse_job_requirements", {"jd_text": "Raw role text"})

    assert len(tools) == 6
    assert job.data.job_id == "J001"
    assert db_path.exists()
    with pytest.raises(sqlite3.ProgrammingError):
        database.connection.execute("SELECT 1")


async def test_default_server_enables_raw_parser_when_provider_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        [
            TurnResult(
                tool_calls=[
                    ToolCall(
                        id="parse",
                        name=PARSER_SUBMIT_TOOL,
                        arguments={
                            "company_name": "Example",
                            "title": "Engineer",
                            "requirements": [
                                {
                                    "text": "Python",
                                    "category": "skill",
                                    "importance": "must",
                                    "normalized_skill": "Python",
                                }
                            ],
                        },
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(server_module, "build_jobintel_provider", lambda _settings: provider)
    server = server_module.build_default_server(
        JobIntelSettings(jobintel_db_path=tmp_path / "raw-mcp.db")
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "parse_job_requirements", {"jd_text": "Python is required."}
        )

    assert result.data.job_id.startswith("job_raw_")
    assert len(result.data.requirements) == 1


def test_default_server_closes_database_when_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = JobIntelDatabase.connect(tmp_path / "failed.db")
    monkeypatch.setattr(
        server_module.JobIntelDatabase,
        "connect",
        lambda _path: database,
    )
    monkeypatch.setattr(
        server_module.MigrationRunner,
        "migrate",
        lambda _self: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        server_module.build_default_server(
            JobIntelSettings(jobintel_db_path=tmp_path / "failed.db")
        )

    with pytest.raises(sqlite3.ProgrammingError):
        database.connection.execute("SELECT 1")


def test_main_runs_default_server(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = False

    def run() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(
        server_module,
        "build_default_server",
        lambda: SimpleNamespace(run=run),
    )

    server_module.main()

    assert ran is True
