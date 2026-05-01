"""NewsAPI adapter for optional target-related article discovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import Settings
from src.domain import ResolvedTarget, SourceDocument, SourceStrength, SourceType


class NewsApiClient:
    """Discovers target-related news metadata for later evidence enrichment."""

    everything_url = "https://newsapi.org/v2/everything"

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds)

    def fetch_target_articles(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 5,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        response = self.http_client.get(
            self.everything_url,
            params={
                "q": f'"{target.canonical_name}" OR {target.ticker}',
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": page_size,
                "apiKey": self.settings.news_api_key,
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

        documents: list[SourceDocument] = []
        for article in payload.get("articles", []):
            url = article.get("url")
            if not url:
                continue
            documents.append(
                SourceDocument(
                    source_id="newsapi",
                    source_dimension=source_dimension,
                    source_type=SourceType.news_article,
                    source_strength=source_strength,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url=url,
                    metadata=article,
                    retrieved_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
                )
            )
        return documents
