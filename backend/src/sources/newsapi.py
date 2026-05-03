"""NewsAPI adapter for optional target-related article discovery."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import Settings
from src.domain import ResolvedTarget, SourceDocument, SourceStrength, SourceType


logger = logging.getLogger(__name__)
uvicorn_console_logger = logging.getLogger("uvicorn.error")
# Keep NewsAPI request diagnostics visible even when the application root logger stays at WARNING.
logger.setLevel(logging.INFO)
uvicorn_console_logger.setLevel(logging.INFO)


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

        response = self.http_client.get(
            self.everything_url,
            params=params,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        _log_info(
            "NewsAPI response: status=%s total_results=%s article_count=%s",
            payload.get("status"),
            payload.get("totalResults"),
            len(payload.get("articles", [])),
        )

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


def _log_info(message: str, *args: Any) -> None:
    """Emit NewsAPI diagnostics to both module logs and the Uvicorn console."""

    logger.info(message, *args)
    uvicorn_console_logger.info(message, *args)


def _redacted_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return log-safe request params without exposing provider credentials."""

    return {key: ("<redacted>" if key == "apiKey" else value) for key, value in params.items()}
