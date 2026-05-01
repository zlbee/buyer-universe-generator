# Buyer Universe Generator Backend

This backend package contains the FastAPI application, CLI, domain contracts, SQLAlchemy persistence setup, source adapter interfaces, retriever interfaces, LLM abstraction, and exporter interfaces for the Buyer Universe Generator.

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

Run CLI helpers:

```powershell
uv run bug show-config
uv run bug init-db
```
