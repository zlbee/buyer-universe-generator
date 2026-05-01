# Buyer Universe Generator

Buyer Universe Generator is a public-data MVP for generating a traceable buyer long-list for US-listed targets. Phase 0 establishes the runnable project foundation: backend package structure, frontend shell, shared domain contracts, configuration, SQLite persistence, Docker Compose orchestration, and smoke tests.

## Current Implementation Status

Implemented:

- Python 3.12 project metadata and dependency declarations.
- Backend source layout under `backend/`.
- FastAPI application entrypoint with a health endpoint.
- React 18 + TypeScript + Vite frontend shell.
- CLI entrypoint for database initialization and config inspection.
- `pydantic-settings` configuration.
- SQLAlchemy 2.0 SQLite bootstrap.
- Docker Compose setup for backend and frontend services.
- Base domain models for evidence, target profiles, candidate hits, long-list candidates, and pipeline runs.
- Pytest smoke test for configuration, JSON serialization, SQLite initialization, and API startup.

Not implemented yet:

- Target resolution and source ingestion.
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

## Run The API

```powershell
cd backend
uv run uvicorn src.api.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000/health
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
