# Buyer Universe Generator

Buyer Universe Generator is a public-data MVP for creating an evidence-backed buyer candidate pool for US-listed targets. Given a ticker or exact company name, it resolves the target, builds a multi-source `TargetProfile`, and recalls first-pass strategic and financial buyer candidates with source evidence.

The current project is a high-recall candidate-generation foundation, not a finished banker-grade buyer universe. Normalization, deduplication, hard filters, scoring, reranking, and final export/reporting are still planned work.

## Current Status

Implemented:

- FastAPI backend, React + Vite frontend, and Docker Compose startup.
- Target resolution and source ingestion through SEC EDGAR.
- Evidence-backed `TargetProfile` extraction with OpenRouter LLM support, feature labels, feature evidence, and profile caching.
- Config-driven retrieval rules in `backend/config/retrieval_rules.yaml`.
- Optional enrichers for Polygon.io, company/IR pages, NewsAPI, Google News RSS, Financial Modeling Prep, and OpenRouter web search.
- First-pass strategic buyer recall:
  - `SameSicRetriever`
  - `MAHistoryRetriever`
  - `StrategicAcquisitionIntentRetriever`
  - planned shell retrievers for other strategic paths
- First-pass financial buyer recall:
  - `PEDealActivityRetriever`
  - configurable PE seed universe in `backend/config/pe_seed_universe.yaml`
- SQLite persistence for source cache, profile cache, buyer recall cache, and audit logs.
- Backend tests for settings, source strategy, target profile extraction, strategic recall, financial recall, API behavior, and cache behavior.

Still planned:

- Full candidate normalization and deduplication.
- Hard filters with explainable exclusion or pending-verification reasons.
- Pre-scoring, reranking, and final buyer universe generation.
- Stronger PE portfolio and investment-criteria coverage.
- Export/report workflows.

## Quick Start With Docker

Prerequisite: Docker Desktop or another Docker Compose-compatible runtime.

1. Create a local environment file:

```powershell
Copy-Item .env.example .env
```

2. Edit `.env` and set the keys you want to use.

`BUG_OPENROUTER_API_KEY` is required for `TargetProfile` extraction and buyer-candidate endpoints. The other keys are optional enrichers:

```text
EDGAR_IDENTITY=buyer-universe-generator/0.1 your-email@example.com
BUG_OPENROUTER_API_KEY=...
BUG_POLYGON_API_KEY=...
BUG_NEWS_API_KEY=...
BUG_FMP_API_KEY=...
```

3. Start both services:

```powershell
docker compose up --build
```

Open:

- Frontend: `http://127.0.0.1:5173`
- Backend API: `http://127.0.0.1:8000`
- Health check: `http://127.0.0.1:8000/health`

Stop services:

```powershell
docker compose down
```

Docker stores runtime data under the repo `data/` directory.

## Useful API Endpoints

```text
GET /health
GET /targets/resolve?query=ELF
GET /targets/profile?query=ELF
GET /buyers/strategic-candidates?query=ELF
GET /buyers/financial-candidates?query=ELF
GET /buyers/candidates?query=ELF
```

The candidate endpoints return raw first-pass hits. They do not yet apply final normalization, hard filters, scoring, or final long-list promotion.

## Local Development

Use this path when you want to run backend and frontend outside Docker.

Backend:

```powershell
cd backend
uv venv
uv pip install -e ".[dev]"
uv run uvicorn src.api.main:app --reload
```

Frontend:

```powershell
cd frontend
npm install
npm run dev
```

CLI helpers:

```powershell
cd backend
uv run bug show-config
uv run bug resolve-target ELF
uv run bug build-target-profile ELF
```

## Tests

Backend tests:

```powershell
cd backend
uv run --extra dev pytest
```

Frontend build check:

```powershell
cd frontend
npm run build
```

## Project Layout

- `backend/`: FastAPI app, domain models, source adapters, retrievers, pipeline orchestration, persistence, and CLI.
- `frontend/`: React + TypeScript + Vite UI.
- `backend/config/retrieval_rules.yaml`: provider routing, evidence profiles, stage use cases, and retriever parameters.
- `backend/config/pe_seed_universe.yaml`: configured PE sponsor identity universe for financial buyer recall.
- `data/`: local runtime database/cache data when using Docker Compose.
