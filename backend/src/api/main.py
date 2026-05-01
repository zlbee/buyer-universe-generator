"""FastAPI application factory and Phase 0 health endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.config import Settings, get_settings
from src.pipelines.factory import build_source_ingestion_service
from src.pipelines.target_resolution import AmbiguousTargetError, TargetNotFoundError
from src.repositories.database import create_session_factory, init_db


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = init_db(active_settings)
        yield

    app = FastAPI(
        title=active_settings.app_name,
        version=__version__,
        debug=active_settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["system"])
    def health_check() -> dict[str, str]:
        return {
            "status": "ok",
            "app": active_settings.app_name,
            "environment": active_settings.environment,
            "version": __version__,
        }

    @app.get("/targets/resolve", tags=["targets"])
    def resolve_target(request: Request, query: str = Query(min_length=1)) -> dict:
        session_factory = create_session_factory(request.app.state.engine)
        with session_factory() as session:
            service = build_source_ingestion_service(active_settings, session)
            try:
                result = service.ingest(query)
            except AmbiguousTargetError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                    },
                ) from error
            except TargetNotFoundError as error:
                raise HTTPException(status_code=404, detail={"message": str(error)}) from error

        return result.model_dump(mode="json")

    return app


app = create_app()
