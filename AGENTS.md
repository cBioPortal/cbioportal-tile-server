# Repository Guidelines

## Project Structure & Module Organization
`app/` contains the FastAPI service code. `app/main.py` is the entrypoint, while modules such as `tiles.py`, `slides.py`, `meta.py`, and `cache.py` hold tile serving, slide access, Databricks metadata, and Redis cache logic. `tests/` contains pytest coverage for API and unit behavior; keep new tests close to the feature they validate. `tools/` holds operational and data-migration scripts, `bench/` contains benchmarking utilities, and `docs/runbook.md` documents deployment and operations.

## Build, Test, and Development Commands
Use `uv` for local Python workflows.

```bash
uv sync --dev
uv run pytest
python3 tools/write_dev_env.py
docker compose up --build
```

`uv sync --dev` installs runtime and test dependencies. `uv run pytest` runs the full test suite. `python3 tools/write_dev_env.py` securely writes the local `.env` file for Docker-based development. `docker compose up --build` starts the tile server and Redis locally on port `8080`.

## Coding Style & Naming Conventions
Target Python 3.11+ and follow the existing style: 4-space indentation, explicit type hints where useful, small focused functions, and module-level constants in `UPPER_SNAKE_CASE`. Use `snake_case` for functions, variables, and test names. Preserve the current FastAPI pattern of thin route handlers delegating to service modules. No formatter or linter is configured in-repo, so match surrounding code closely and keep imports and logging tidy.

## Testing Guidelines
Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = auto`). Add coverage in `tests/test_<feature>.py`; use `tests/conftest.py` for shared fixtures. Prefer focused unit tests for parsing, caching, and metadata behavior, plus API-level tests when routes or response headers change. Run `uv run pytest` before opening a PR.

## Commit & Pull Request Guidelines
Recent history favors short, imperative commit subjects such as `Refactor core tile server internals` or scoped prefixes like `test:` and `security:`. Keep commits focused and descriptive. PRs should explain the behavior change, note config or credential impacts, link related issues, and list local verification steps. Include sample requests or response snippets when changing endpoints or metadata output.

## Security & Configuration Tips
Do not commit `.env`, credentials, or Databricks/AWS secrets. Review changes to cache headers and patient metadata routes carefully because those endpoints can expose PHI and must remain non-cacheable by shared proxies.
