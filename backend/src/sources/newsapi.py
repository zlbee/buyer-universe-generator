"""NewsAPI adapter for optional target-related article discovery."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from src.config import Settings
from src.domain import DataSourceRawRecord, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient, source_document_from_raw_record


logger = logging.getLogger(__name__)


class NewsApiClient(ExternalDataSourceClient):
    """Discovers target-related news metadata for later evidence enrichment."""

    everything_url = "https://newsapi.org/v2/everything"

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "newsapi.org",
    ) -> None:
        super().__init__(
            settings,
            source_id="newsapi",
            provider=provider,
            http_client=http_client,
            request_recorder=request_recorder,
        )

    def fetch_target_articles(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 5,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
        domains: Sequence[str] | None = None,
    ) -> list[SourceDocument]:
        params: dict[str, Any] = {
            "q": f'"{target.canonical_name}" OR {target.ticker}',
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": page_size,
            "apiKey": self.settings.news_api_key,
        }
        if domains:
            # NewsAPI expects a comma-delimited allowlist in the `domains` query parameter.
            params["domains"] = ",".join(domains)

        _log_info("NewsAPI request: url=%s params=%s", self.everything_url, _redacted_params(params))

        payload, raw_records = self._get_json(
            operation="everything",
            url=self.everything_url,
            params=params,
            target=target,
            source_dimension=source_dimension,
            raw_records_from_payload=_newsapi_raw_records_from_payload,
        )
        _log_info(
            "NewsAPI response: status=%s total_results=%s article_count=%s",
            payload.get("status"),
            payload.get("totalResults"),
            len(payload.get("articles", [])),
        )

        return [
            source_document_from_raw_record(
                raw_record,
                source_strength=source_strength,
                ttl_hours=ttl_hours,
            )
            for raw_record in raw_records
            if raw_record.url
        ]


def _log_info(message: str, *args: Any) -> None:
    """Emit NewsAPI diagnostics through the shared application logging setup."""

    logger.info(message, *args)


def _redacted_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return log-safe request params without exposing provider credentials."""

    return {key: ("<redacted>" if key == "apiKey" else value) for key, value in params.items()}


def _newsapi_raw_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    if not isinstance(payload, dict):
        return []

    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []

    raw_records: list[DataSourceRawRecord] = []
    for index, article in enumerate(articles, start=1):
        if not isinstance(article, dict):
            continue
        raw_records.append(
            DataSourceRawRecord(
                request_id=context.request_id,
                source_id=context.source_id,
                provider=context.provider,
                source_dimension=context.source_dimension,
                source_type=SourceType.news_article,
                target_cik=context.target_cik,
                target_ticker=context.target_ticker,
                record_id=str(article.get("url") or article.get("title") or index),
                url=article.get("url") if isinstance(article.get("url"), str) else None,
                title=article.get("title") if isinstance(article.get("title"), str) else None,
                published_at=article.get("publishedAt") if isinstance(article.get("publishedAt"), str) else None,
                raw_payload=article,
                metadata={
                    "provider_status": payload.get("status"),
                    "provider_total_results": payload.get("totalResults"),
                },
                retrieved_at=retrieved_at,
            )
        )
    return raw_records
