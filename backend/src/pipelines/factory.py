"""Factory helpers for Phase 1 services."""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.config import Settings
from src.pipelines.source_ingestion import SourceIngestionService
from src.pipelines.target_resolution import TargetResolver
from src.repositories.source_cache import SourceCache
from src.sources.registry import SourceRegistry
from src.sources.strategy import DataSourceStrategy


def build_source_ingestion_service(settings: Settings, session: Session) -> SourceIngestionService:
    strategy = DataSourceStrategy.from_settings(settings)
    registry = SourceRegistry(settings)
    cache = SourceCache(session)
    edgar_client = registry.edgar()
    resolver = TargetResolver(strategy=strategy, edgar_client=edgar_client, source_cache=cache)
    return SourceIngestionService(
        strategy=strategy,
        resolver=resolver,
        cache=cache,
        edgar_client=edgar_client,
        polygon_client=registry.polygon(),
        newsapi_client=registry.newsapi(),
    )
