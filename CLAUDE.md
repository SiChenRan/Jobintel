# CLAUDE.md

This repository contains only JobIntel, a Python 3.12+ local-first job discovery and analysis application. It uses a `src/` layout, `uv`, Hatchling, Typer, FastAPI, FastMCP, Pydantic, and SQLite.

## Development commands

```bash
make install          # install runtime and development dependencies
make seed             # seed the configured JobIntel database
make web              # run the local web application
make analyze          # analyze the seeded candidate/job pair
make discover         # run a conservative BOSS discovery example
make serve-mcp        # start the FastMCP stdio server
make check            # lint, format check, strict mypy, tests and coverage
make package          # run checks and build wheel/sdist
```

Focused tests can be run with `uv run pytest tests/jobintel/<file>.py`. Tests use temporary or in-memory SQLite databases and fake provider clients, and must not require network access or API keys.

## Architecture

- `jobintel.models` defines immutable domain models and versioned identities.
- `jobintel.persistence` owns SQLite migrations, repositories, and seed loading.
- `jobintel.services` contains resume/JD parsing, evidence retrieval, analysis, discovery analysis, and radar workflows.
- `jobintel.agent` owns the provider-neutral tool loop, guardrails, provenance, and finalization.
- `jobintel.providers` contains the neutral provider protocol plus Anthropic, OpenAI, and DeepSeek adapters.
- `jobintel.tool_contracts` is the single source for in-process and FastMCP tool schemas.
- `jobintel.outreach` owns HR outreach draft contracts, stable identities, review-state transitions, channel rendering, prompts, and deterministic evidence guardrails. It must not operate BOSS chat controls.
- `jobintel.notifications` owns candidate-scoped SMTP delivery for saved discovery results. Recipient addresses are persisted only as candidate preferences; delivery endpoints must resolve them from the discovery's candidate and must not accept recipient overrides. Never persist message bodies or SMTP credentials.
- `jobintel.discovery` contains the Chrome CDP transport and BOSS connector. Preserve conservative pacing, caching, and risk controls.
- `jobintel.web` exposes local HTTP APIs and the bundled static frontend.

The model may draft qualitative analysis, but deterministic scores, recommendation thresholds, identities, timestamps, persistence, and provenance are controlled by application code. Scoring V2 includes only named professional skills and explicit education, language, experience, or technical-project thresholds; generic duties, soft skills, and product outcomes remain visible context with zero scoring weight. Do not permit the model to invent requirement IDs or evidence references.

## Local data and safety

Runtime secrets live in `.env`; never print or commit it. `data/jobintel.db`, generated profile previews, and Chrome profiles are local user data and must stay untracked. Seed fixtures under `data/jobintel_seed/` are versioned.

BOSS access must use the user's authenticated Chrome session through loopback CDP. Do not expose the debugging port publicly, bypass verification, add stealth/evasion behavior, or raise default request concurrency and frequency without explicit justification and tests.
