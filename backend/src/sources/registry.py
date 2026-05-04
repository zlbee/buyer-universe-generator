"""Factory for live Phase 1 source adapters."""

from __future__ import annotations

from src.config import Settings
from src.sources.company_pages import CompanyPageClient
from src.sources.base import DataSourceRequestRecorder
from src.sources.google_news_rss import GoogleNewsRssClient
from src.sources.newsapi import NewsApiClient
from src.sources.polygon import PolygonClient
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy


class SourceRegistry:
    """Central adapter registry so source providers can be swapped in one place."""

    def __init__(
        self,
        settings: Settings,
        strategy: DataSourceStrategy | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.request_recorder = request_recorder

    def edgar(self) -> SecEdgarClient:
        retrieval = self._retrieval("edgar")
        return SecEdgarClient(
            self.settings,
            request_recorder=self.request_recorder,
            provider=self._provider("edgar", "edgartools"),
            markdown_item_parser_enabled=retrieval.markdown_item_parser_enabled,
            markdown_item_parser_min_chars=retrieval.markdown_item_parser_min_chars,
        )

    def polygon(self) -> PolygonClient:
        return PolygonClient(
            self.settings,
            request_recorder=self.request_recorder,
            provider=self._provider("polygon", "polygon.io"),
        )

    def company_pages(self) -> CompanyPageClient | None:
        if not self.settings.enable_company_pages:
            return None
        return CompanyPageClient(
            self.settings,
            request_recorder=self.request_recorder,
            provider=self._provider("company_pages", "official_investor_relations_pages"),
        )

    def newsapi(self) -> NewsApiClient:
        return NewsApiClient(
            self.settings,
            request_recorder=self.request_recorder,
            provider=self._provider("newsapi", "newsapi.org"),
        )

    def google_news_rss(self) -> GoogleNewsRssClient:
        retrieval = self._retrieval("google_news_rss")
        return GoogleNewsRssClient(
            self.settings,
            request_recorder=self.request_recorder,
            provider=self._provider("google_news_rss", "google_news_rss"),
            default_language=retrieval.rss_language,
            default_country=retrieval.rss_country,
            default_edition=retrieval.rss_edition,
        )

    def _provider(self, source_id: str, fallback: str) -> str:
        if not self.strategy:
            return fallback
        return self.strategy.source(source_id).provider

    def _retrieval(self, source_id: str):
        if not self.strategy:
            from src.domain import DataSourceRetrievalConfig

            return DataSourceRetrievalConfig()
        return self.strategy.source(source_id).retrieval
