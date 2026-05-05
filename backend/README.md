# Buyer Universe Generator Backend

This backend package contains the FastAPI application, CLI, domain contracts, SQLAlchemy persistence setup, source adapters, retriever interfaces, LLM abstraction, and exporter interfaces for the Buyer Universe Generator.

Phase 1 adds target resolution and source ingestion. Phase 2 adds required-LLM TargetProfile extraction from SEC filing text and optional structured enrichers. Phase 5 adds first-pass financial buyer recall through an FMP-backed PE deal-activity retriever. SEC/EDGAR is the required primary source; Polygon.io, FMP, and NewsAPI are optional enrichers controlled by `config/datasources.yaml` and API key settings.

## Local Commands

Install backend dependencies:

```powershell
uv venv
uv pip install -e ".[dev]"
```

Run tests:

```powershell
uv run --extra dev pytest
```

Run the API:

```powershell
uv run uvicorn src.api.main:app --reload
```

Resolve a target through the CLI:

```powershell
uv run bug resolve-target AAPL
```

Build a TargetProfile through the CLI:

```powershell
uv run bug build-target-profile ELF
```

Resolve a target through the API:

```text
GET /targets/resolve?query=AAPL
```

Build a TargetProfile through the API:

```text
GET /targets/profile?query=ELF
```

Retrieve first-pass financial buyer candidates through the API:

```text
GET /buyers/financial-candidates?query=ELF
```

Run CLI helpers:

```powershell
uv run bug show-config
uv run bug init-db
```

Required and optional source keys:

```powershell
$env:BUG_OPENROUTER_API_KEY = "..."
$env:BUG_POLYGON_API_KEY = "..."
$env:BUG_FMP_API_KEY = "..."
$env:BUG_NEWS_API_KEY = "..."
$env:BUG_LOG_LEVEL = "INFO"
```

Phase 5 PE deal activity uses `config/pe_seed_universe.yaml` to recognize configured PE sponsors in FMP M&A records. The seed list is only an identity filter; candidates still require recent target-industry deal evidence from FMP before a `CandidateHit` is emitted.
