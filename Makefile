.PHONY: install seed web analyze discover setup-browser source-doctor serve-mcp \
	test lint format format-check typecheck check package clean

# Install runtime and development dependencies into a uv-managed environment.
install:
	uv sync --all-extras

# Seed the file-backed JobIntel database from versioned fixtures.
seed:
	uv run jobintel seed

# Open the local JobIntel web application.
web:
	uv run jobintel web

# Analyze the default seeded job/candidate pair.
analyze:
	uv run jobintel analyze --candidate-id C001 --job-id J001

# Batch-discover live jobs for the seeded candidate profile.
discover:
	uv run jobintel discover --candidate-id C001 --query "Python 后端" --city 上海

# Launch and diagnose the isolated Chrome bridge used by BOSS discovery.
setup-browser:
	uv run jobintel setup-browser

source-doctor:
	uv run jobintel source-doctor

# Launch the custom MCP server over stdio (for MCP Inspector / Claude Desktop).
serve-mcp:
	uv run jobintel serve-mcp

# Run the offline test suite with coverage (fails under 85%).
test:
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=85

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run mypy

# Everything CI runs, locally.
check: lint format-check typecheck test

package: check
	uv build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
