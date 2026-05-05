"""FastAPI application factory and Phase 0 health endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.config import Settings, get_settings
from src.logging_config import configure_logging
from src.pipelines.factory import (
    build_financial_buyer_candidate_retriever,
    build_source_ingestion_service,
    build_strategic_buyer_candidate_retriever,
    build_target_profile_extractor,
)
from src.pipelines.target_profile_extraction import TargetProfileExtractionError
from src.pipelines.target_resolution import AmbiguousTargetError, TargetNotFoundError
from src.repositories.database import create_session_factory, init_db

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)

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

    @app.get("/targets/profile", tags=["targets"])
    def build_target_profile(request: Request, query: str = Query(min_length=1)) -> dict:
        session_factory = create_session_factory(request.app.state.engine)
        with session_factory() as session:
            service = build_target_profile_extractor(active_settings, session)
            try:
                result = service.build_profile(query)
            except AmbiguousTargetError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "error_code": "ambiguous_target",
                        "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                    },
                ) from error
            except TargetNotFoundError as error:
                raise HTTPException(status_code=404, detail={"message": str(error), "error_code": "target_not_found"}) from error
            except TargetProfileExtractionError as error:
                logger.warning(
                    "TargetProfile API request failed: query=%s status_code=%s error_code=%s details=%s",
                    query,
                    error.status_code,
                    error.error_code,
                    error.details,
                )
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"message": error.message, "error_code": error.error_code, **error.details},
                ) from error

        return result.model_dump(mode="json")

    @app.get("/buyers/strategic-candidates", tags=["buyers"])
    def retrieve_strategic_candidates(request: Request, query: str = Query(min_length=1)) -> dict:
        session_factory = create_session_factory(request.app.state.engine)
        with session_factory() as session:
            profile_service = build_target_profile_extractor(active_settings, session)
            retriever = build_strategic_buyer_candidate_retriever(active_settings, session)
            try:
                profile_result = profile_service.build_profile(query)
                retrieval_result = retriever.retrieve(profile_result.target_profile)
            except AmbiguousTargetError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "error_code": "ambiguous_target",
                        "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                    },
                ) from error
            except TargetNotFoundError as error:
                raise HTTPException(status_code=404, detail={"message": str(error), "error_code": "target_not_found"}) from error
            except TargetProfileExtractionError as error:
                logger.warning(
                    "Strategic candidate retrieval failed during profile extraction: query=%s status_code=%s error_code=%s details=%s",
                    query,
                    error.status_code,
                    error.error_code,
                    error.details,
                )
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"message": error.message, "error_code": error.error_code, **error.details},
                ) from error

        return {
            "target_profile": profile_result.target_profile.model_dump(mode="json"),
            "hits": [hit.model_dump(mode="json") for hit in retrieval_result.hits],
            "warnings": [*profile_result.warnings, *retrieval_result.warnings],
            "metadata": {
                "profile": profile_result.extraction_metadata,
                "retrieval": retrieval_result.metadata,
            },
        }

    @app.get("/buyers/candidates", tags=["buyers"])
    def retrieve_buyer_candidates(request: Request, query: str = Query(min_length=1)) -> dict:
        session_factory = create_session_factory(request.app.state.engine)
        with session_factory() as session:
            profile_service = build_target_profile_extractor(active_settings, session)
            strategic_retriever = build_strategic_buyer_candidate_retriever(active_settings, session)
            financial_retriever = build_financial_buyer_candidate_retriever(active_settings, session)
            try:
                profile_result = profile_service.build_profile(query)
                strategic_result = strategic_retriever.retrieve(profile_result.target_profile)
                financial_result = financial_retriever.retrieve(profile_result.target_profile)
            except AmbiguousTargetError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "error_code": "ambiguous_target",
                        "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                    },
                ) from error
            except TargetNotFoundError as error:
                raise HTTPException(status_code=404, detail={"message": str(error), "error_code": "target_not_found"}) from error
            except TargetProfileExtractionError as error:
                logger.warning(
                    "Buyer candidate retrieval failed during profile extraction: query=%s status_code=%s error_code=%s details=%s",
                    query,
                    error.status_code,
                    error.error_code,
                    error.details,
                )
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"message": error.message, "error_code": error.error_code, **error.details},
                ) from error

        hits = [*strategic_result.hits, *financial_result.hits]
        return {
            "target_profile": profile_result.target_profile.model_dump(mode="json"),
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "warnings": [*profile_result.warnings, *strategic_result.warnings, *financial_result.warnings],
            "metadata": {
                "profile": profile_result.extraction_metadata,
                "retrieval": {
                    "retriever": "BuyerCandidateRetriever",
                    "hit_count": len(hits),
                    "strategic": strategic_result.metadata,
                    "financial": financial_result.metadata,
                },
            },
        }

    @app.get("/buyers/financial-candidates", tags=["buyers"])
    def retrieve_financial_candidates(request: Request, query: str = Query(min_length=1)) -> dict:
        session_factory = create_session_factory(request.app.state.engine)
        with session_factory() as session:
            profile_service = build_target_profile_extractor(active_settings, session)
            retriever = build_financial_buyer_candidate_retriever(active_settings, session)
            try:
                profile_result = profile_service.build_profile(query)
                retrieval_result = retriever.retrieve(profile_result.target_profile)
            except AmbiguousTargetError as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": str(error),
                        "error_code": "ambiguous_target",
                        "candidates": [candidate.model_dump(mode="json") for candidate in error.candidates],
                    },
                ) from error
            except TargetNotFoundError as error:
                raise HTTPException(status_code=404, detail={"message": str(error), "error_code": "target_not_found"}) from error
            except TargetProfileExtractionError as error:
                logger.warning(
                    "Financial candidate retrieval failed during profile extraction: query=%s status_code=%s error_code=%s details=%s",
                    query,
                    error.status_code,
                    error.error_code,
                    error.details,
                )
                raise HTTPException(
                    status_code=error.status_code,
                    detail={"message": error.message, "error_code": error.error_code, **error.details},
                ) from error

        return {
            "target_profile": profile_result.target_profile.model_dump(mode="json"),
            "hits": [hit.model_dump(mode="json") for hit in retrieval_result.hits],
            "warnings": [*profile_result.warnings, *retrieval_result.warnings],
            "metadata": {
                "profile": profile_result.extraction_metadata,
                "retrieval": retrieval_result.metadata,
            },
        }

    return app


app = create_app()
