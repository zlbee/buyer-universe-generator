"""Polygon.io adapter for optional target exchange/profile enrichment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import Settings
from src.domain import ResolvedTarget, SourceDocument, SourceStrength, SourceType


class PolygonClient:
    """Fetches supporting market/profile metadata when a Polygon API key is configured."""

    base_url = "https://api.polygon.io"

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds)

    def fetch_ticker_profile(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        response = self.http_client.get(
            f"{self.base_url}/v3/reference/tickers/{target.ticker}",
            params={"apiKey": self.settings.polygon_api_key},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        result = payload.get("results", payload)
        return SourceDocument(
            source_id="polygon",
            source_dimension=source_dimension,
            source_type=SourceType.exchange_profile,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=f"{self.base_url}/v3/reference/tickers/{target.ticker}",
            metadata=result,
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )
