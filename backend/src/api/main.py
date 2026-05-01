"""FastAPI application factory and Phase 0 health endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.config import Settings, get_settings
from src.repositories.database import init_db


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

    return app


app = create_app()
