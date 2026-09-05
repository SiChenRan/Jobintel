# ADR 0001: JobIntel V1 architecture

- Status: Accepted
- Date: 2026-09-04
- Updated: 2026-09-05

## Context

JobIntel combines private candidate data, live job discovery, model-assisted analysis, deterministic scoring, and persistent local history. It needs one domain architecture across CLI, Web, and MCP without allowing provider-specific behavior or model output to control trusted application state.

## Decision

1. JobIntel is the repository's only application package and is published from `src/jobintel`.
2. Provider adapters implement a stateless single-turn protocol. Transcript ownership, tool orchestration, validation, scoring, and persistence remain provider-neutral.
3. Job and candidate-profile identities are immutable and versioned. Requirement IDs are generated from canonical content by application code.
4. Model-authored analysis drafts are separated from finalized analyses. Scores, recommendations, timestamps, identities, and version metadata are controlled by code.
5. Deterministic scoring uses Decimal arithmetic, explicit importance weights, and one final `ROUND_HALF_UP` operation.
6. JobIntel state is stored in a local SQLite database. Migrations and repository methods are the only persistence boundary.
7. In-process and FastMCP adapters share one tool-contract source and call the same application services.
8. BOSS discovery runs through the user's explicit Chrome CDP session with conservative pacing, bounded concurrency, detail caching, and radar cooldowns.
9. The Web UI is a thin local client of the same application services used by the CLI; it does not contain independent business logic.

## Consequences

- Anthropic, OpenAI, and DeepSeek adapters can change independently without leaking SDK types into the application layer.
- CLI, Web, and MCP behavior stays aligned through shared services and contracts.
- Tests can run offline using temporary databases and injected provider/CDP clients.
- Browser access remains visible and user-controlled; the project does not implement verification bypass or stealth automation.
- Schema, scoring, and tool-contract changes require explicit versioning and regression tests.
