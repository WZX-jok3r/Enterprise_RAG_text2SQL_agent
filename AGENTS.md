# Repository Guidelines

## Project Structure & Module Organization

This repository implements a multi-tenant knowledge agent combining RAG retrieval and Text2SQL.

- `backend/app/`: FastAPI APIs, agent orchestration, retrieval, analytics, persistence, and ARQ ingestion workers.
- `backend/tests/`: backend tests; `backend/eval/`: retrieval, answer-quality, and performance evaluation tools.
- `backend/migrations/`: Alembic schema migrations.
- `frontend/src/`: Vue 3 and TypeScript components, Pinia stores, API clients, and styles.
- `knowledge_base/` and `test_dataset/`: source documents and evaluation data.
- `docker_setting/`, `scripts/`, and `docs/`: deployment, verification, and design/operations documentation.

Prefer the active `backend/app/` implementation over the older root-level `app/` and `src/` modules.

## Build, Test, and Development Commands

Use Python 3.12+. Backend commands below run from `backend/` with the project virtual environment activated:

- `pip install -e .` and `pip install pytest pytest-asyncio`: install application and test dependencies.
- `uvicorn app.main:app --reload`: start the development API.
- `alembic upgrade head`: apply database migrations.
- `arq app.worker.settings.WorkerSettings`: start the ingestion worker.
- `python -m pytest -q`: run backend tests.

From `frontend/`, run `npm ci`, then `npm run dev` for development or `npm run build` for TypeScript validation and production output.

From the repository root:

- `docker compose -f docker_setting/docker-compose.yml up -d --build`: build and start the stack.
- `.\scripts\check.ps1`: compile Python modules and run tests using root `.venv`.
- `.\scripts\check.ps1 -Full`: additionally evaluate retrieval against the per-question baseline; required for retrieval, chunking, ingestion, or evaluation changes. Requires running infrastructure and indexed Qdrant.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/modules, and `PascalCase` classes. Match existing TypeScript formatting: two spaces, semicolons, `camelCase` variables, and `PascalCase.vue` components. Keep PowerShell scripts ASCII-only for Windows PowerShell compatibility. No formatter or linter is configured.

## Testing Guidelines

Use pytest and pytest-asyncio; async mode is automatic. Name files `test_*.py` and functions `test_*`. Add regression tests for changed behavior, especially tenant isolation, SQL permissions, concurrency, and version visibility. Service-dependent tests may skip without credentials or databases; report these skips. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Follow history: `feat(scope): description`, `fix(scope): description`, `docs: description`, or `chore: description`. Keep commits focused. PRs should explain the behavior change, link relevant issues, report validation and skips, and include screenshots for UI changes. Document migrations and deployment steps.

## Security & Configuration

Start from `.env.example`; never commit credentials or private uploaded data. Root `.env` is overridden by `backend/.env`. Preserve fail-closed tenant isolation, read-only SQL enforcement, and role-based permissions.
