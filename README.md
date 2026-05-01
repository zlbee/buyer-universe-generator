# Buyer Universe Generator

Buyer Universe Generator is a public-data MVP for generating a traceable buyer long-list for US-listed targets. Phase 0 establishes the runnable project foundation: backend package structure, frontend shell, shared domain contracts, configuration, SQLite persistence, Docker Compose orchestration, and smoke tests.

## Current Implementation Status

Implemented:

- Python 3.12 project metadata and dependency declarations.
- Backend source layout under `backend/`.
- FastAPI application entrypoint with a health endpoint.
- Phase 1 target resolution and source ingestion endpoint.
- React 18 + TypeScript + Vite frontend shell.
- CLI entrypoint for database initialization and config inspection.
- Configurable data-source policy for EDGAR, Polygon.io, and NewsAPI.
- `pydantic-settings` configuration.
- SQLAlchemy 2.0 SQLite bootstrap.
- Docker Compose setup for backend and frontend services.
- Base domain models for evidence, target profiles, candidate hits, long-list candidates, and pipeline runs.
- Pytest smoke test for configuration, JSON serialization, SQLite initialization, and API startup.

Not implemented yet:

- Target feature extraction.
- Buyer candidate retrieval.
- Long-list filtering, scoring, and export.

## Prerequisites

- Python 3.12
- Node.js 22 or newer
- `uv` is recommended for environment and dependency management.
- Docker Desktop or another Docker Compose-compatible runtime if running containers.

## Setup

Repository layout:

- `backend/`: FastAPI application, domain contracts, persistence, source adapters, retrievers, exporters, and CLI.
- `frontend/`: React 18 + TypeScript + Vite application.
- `backend/tests/`: backend smoke and contract tests.

```powershell
cd backend
uv venv
uv pip install -e ".[dev]"
cd ..
```

Install frontend dependencies:

```powershell
cd frontend
npm install
cd ..
```

If you prefer `pip`:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd ..
```

## Configuration

Copy `backend/.env.example` to `backend/.env` and adjust values as needed.

All application settings use the `BUG_` environment variable prefix. For example:

```powershell
$env:BUG_DATABASE_URL = "sqlite:///./data/buyer_universe.db"
```

EDGAR identity can be configured with either `EDGAR_IDENTITY` or `BUG_EDGAR_IDENTITY`.
Polygon.io and NewsAPI are optional Phase 1 enrichers:

```powershell
$env:BUG_POLYGON_API_KEY = "..."
$env:BUG_NEWS_API_KEY = "..."
```

Data-source enablement, use-case routing, and dimension-scoped source strength are configured in:

```text
backend/config/datasources.yaml
```

## Run The API

```powershell
cd backend
uv run uvicorn src.api.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000/health
```

Resolve and ingest source metadata for a target:

```text
http://127.0.0.1:8000/targets/resolve?query=AAPL
```

## Run The Frontend

```powershell
cd frontend
npm run dev
```

Then open:

```text
http://127.0.0.1:5173
```

The frontend calls the backend health endpoint through `VITE_API_BASE_URL`. By default it uses:

```text
http://127.0.0.1:8000
```

The home page includes a TargetProfile Debug entry. Enter a ticker or exact company name,
then run the debug profile action to inspect the resolved target, TargetProfile draft,
SEC filings, source documents, warnings, and raw API response returned by
`/targets/resolve`.

## Run The CLI

Initialize SQLite and create base tables:

```powershell
cd backend
uv run bug init-db
```

Show effective configuration, excluding secrets:

```powershell
cd backend
uv run bug show-config
```

Resolve a target and cache Phase 1 source metadata:

```powershell
cd backend
uv run bug resolve-target AAPL
```

The same commands can be run without installing the console script:

```powershell
cd backend
uv run python -m src init-db
uv run python -m src show-config
```

## Run With Docker Compose

Build and start the backend and frontend services:

```powershell
docker compose up --build
```

Service URLs:

- Backend API: `http://127.0.0.1:8000`
- Backend health check: `http://127.0.0.1:8000/health`
- Frontend: `http://127.0.0.1:5173`

Stop services:

```powershell
docker compose down
```

SQLite data is stored in the `backend-data` Docker volume.

## Tests

```powershell
cd backend
uv run --extra dev pytest
```

Build the frontend:

```powershell
cd frontend
npm run build
```

The Phase 0 smoke test validates:

- Settings load with defaults and overrides.
- Domain models serialize to JSON-compatible dictionaries.
- SQLite initializes without manual steps.
- The FastAPI health endpoint starts successfully.
- Data-source policy disables optional sources when API keys are missing.
- SEC/Polygon/NewsAPI adapters are covered with mocked responses.
- Target resolution and source cache reuse are covered with deterministic fixtures.
