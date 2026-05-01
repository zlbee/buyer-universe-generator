"""Factory for live Phase 1 source adapters."""

from __future__ import annotations

from src.config import Settings
from src.sources.newsapi import NewsApiClient
from src.sources.polygon import PolygonClient
from src.sources.sec import SecEdgarClient


class SourceRegistry:
    """Central adapter registry so source providers can be swapped in one place."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def edgar(self) -> SecEdgarClient:
        return SecEdgarClient(self.settings)

    def polygon(self) -> PolygonClient:
        return PolygonClient(self.settings)

    def newsapi(self) -> NewsApiClient:
        return NewsApiClient(self.settings)
