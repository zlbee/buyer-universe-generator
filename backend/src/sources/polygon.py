"""Polygon.io adapter for optional target exchange/profile enrichment."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from src.config import Settings
from src.domain import DataSourceRawRecord, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient, source_document_from_raw_record


class PolygonClient(ExternalDataSourceClient):
    """Fetches supporting market/profile metadata when a Polygon API key is configured."""

    base_url = "https://api.polygon.io"

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "polygon.io",
    ) -> None:
        super().__init__(
            settings,
            source_id="polygon",
            provider=provider,
            http_client=http_client,
            request_recorder=request_recorder,
        )

    def fetch_ticker_profile(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        url = f"{self.base_url}/v3/reference/tickers/{target.ticker}"
        _payload, raw_records = self._get_json(
            operation="ticker_profile",
            url=url,
            params={"apiKey": self.settings.polygon_api_key},
            target=target,
            source_dimension=source_dimension,
            raw_records_from_payload=_polygon_raw_records_from_payload,
        )
        return source_document_from_raw_record(
            raw_records[0],
            source_strength=source_strength,
            ttl_hours=ttl_hours,
        )


def _polygon_raw_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    result = payload.get("results", payload) if isinstance(payload, dict) else {"value": payload}
    if not isinstance(result, dict):
        result = {"value": result}

    return [
        DataSourceRawRecord(
            request_id=context.request_id,
            source_id=context.source_id,
            provider=context.provider,
            source_dimension=context.source_dimension,
            source_type=SourceType.exchange_profile,
            target_cik=context.target_cik,
            target_ticker=context.target_ticker,
            record_id=str(result.get("ticker") or context.target_ticker or context.url),
            url=context.url,
            raw_payload=result,
            metadata={"provider_payload_status": payload.get("status")} if isinstance(payload, dict) else {},
            retrieved_at=retrieved_at,
        )
    ]
