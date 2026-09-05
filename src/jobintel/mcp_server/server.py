"""Thin FastMCP transport adapter over the in-process JobIntel toolbox."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import Lifespan, lifespan
from fastmcp.tools import FunctionTool
from mcp.types import ToolAnnotations

from jobintel.agent.tools import JobIntelToolbox, ToolExecutionError
from jobintel.config import JobIntelSettings, load_jobintel_settings
from jobintel.persistence.db import JobIntelDatabase
from jobintel.persistence.migrations import MigrationRunner
from jobintel.persistence.repository import SQLiteJobRepository
from jobintel.persistence.seed import seed_database
from jobintel.provenance import ProvenanceLedger
from jobintel.providers.factory import build_jobintel_provider
from jobintel.services.jd_parser import JDParserService
from jobintel.tool_contracts import TOOL_CONTRACTS, ToolContract, ToolEffect


def _annotations(contract: ToolContract) -> ToolAnnotations:
    """Map canonical effects to standard MCP behavioral hints."""
    return ToolAnnotations(
        readOnlyHint=contract.effect is not ToolEffect.WRITE,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _function_tool(
    contract: ToolContract,
    toolbox: JobIntelToolbox,
    call_sequence: itertools.count[int],
) -> FunctionTool:
    """Adapt one canonical contract without deriving a second schema."""

    async def invoke(**arguments: Any) -> Any:
        ordinal = next(call_sequence)
        try:
            return await toolbox.execute(
                contract.name,
                arguments,
                tool_call_id=f"mcp-{ordinal}",
                iteration=ordinal,
            )
        except ToolExecutionError as exc:
            raise ToolError(exc.envelope.model_dump_json()) from exc

    return FunctionTool(
        name=contract.name,
        description=contract.description,
        parameters=contract.input_schema(),
        output_schema=contract.output_schema(),
        annotations=_annotations(contract),
        fn=invoke,
        return_type=contract.response_model,
        run_in_thread=False,
    )


def _database_lifespan(database: JobIntelDatabase) -> Lifespan:
    """Close a server-owned database when its MCP lifespan ends."""

    @lifespan
    async def manage_database(_: FastMCP[Any]) -> AsyncIterator[dict[str, Any] | None]:
        try:
            yield None
        finally:
            database.close()

    return manage_database


def build_server(toolbox: JobIntelToolbox, *, database: JobIntelDatabase | None = None) -> FastMCP:
    """Build an injectable FastMCP server exposing exactly six JobIntel tools."""
    server = FastMCP(
        name="JobIntel",
        instructions=(
            "Retrieve immutable job/profile evidence and submit one guarded terminal "
            "application analysis."
        ),
        version="1.0",
        dereference_schemas=False,
        mask_error_details=False,
        lifespan=_database_lifespan(database) if database is not None else None,
    )
    call_sequence = itertools.count(1)
    for contract in TOOL_CONTRACTS:
        server.add_tool(_function_tool(contract, toolbox, call_sequence))
    return server


def build_default_server(
    settings: JobIntelSettings | None = None,
) -> FastMCP:
    """Build a seeded file-backed MCP server with owned connection lifecycle."""
    configured = settings or load_jobintel_settings()
    database = JobIntelDatabase.connect(configured.jobintel_db_path)
    try:
        MigrationRunner(database).migrate()
        count = int(
            database.connection.execute("SELECT COUNT(*) FROM candidate_profiles").fetchone()[0]
        )
        if count == 0:
            seed_database(database)
        repository = SQLiteJobRepository(database)
        parser: JDParserService | None = None
        try:
            provider = build_jobintel_provider(configured)
        except RuntimeError:
            pass
        else:
            parser = JDParserService(
                provider,
                max_repairs=configured.parser_max_repairs,
            )
        toolbox = JobIntelToolbox(
            repository,
            ProvenanceLedger(f"mcp_server_{uuid.uuid4().hex}"),
            jd_parser=parser,
        )
        return build_server(toolbox, database=database)
    except Exception:
        database.close()
        raise


def main() -> None:
    """Run the default JobIntel MCP server over stdio."""
    build_default_server().run()


if __name__ == "__main__":
    main()
