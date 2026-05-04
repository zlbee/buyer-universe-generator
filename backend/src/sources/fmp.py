"""Financial Modeling Prep adapter for supplemental market, profile, and M&A data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from src.config import Settings
from src.domain import DataSourceRawRecord, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient, source_document_from_raw_record


class FinancialModelingPrepClient(ExternalDataSourceClient):
    """Fetches FMP stable API records when a Financial Modeling Prep API key is configured."""

    base_url = "https://financialmodelingprep.com/stable"

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "financialmodelingprep.com",
    ) -> None:
        super().__init__(
            settings,
            source_id="fmp",
            provider=provider,
            http_client=http_client,
            request_recorder=request_recorder,
        )

    def fetch_company_profile(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        """Fetch FMP company profile data for one ticker symbol."""

        _payload, raw_records = self._get_json(
            operation="company_profile",
            url=f"{self.base_url}/profile",
            params=self._auth_params({"symbol": target.ticker}),
            target=target,
            source_dimension=source_dimension,
            raw_records_from_payload=_fmp_company_profile_records_from_payload,
        )
        if not raw_records:
            raise ValueError(f"FMP profile returned no records for symbol {target.ticker}")
        return source_document_from_raw_record(raw_records[0], source_strength=source_strength, ttl_hours=ttl_hours)

    def fetch_company_profile_by_cik(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        """Fetch FMP company profile data by SEC CIK when ticker mapping is uncertain."""

        _payload, raw_records = self._get_json(
            operation="company_profile_by_cik",
            url=f"{self.base_url}/profile-cik",
            params=self._auth_params({"cik": target.cik}),
            target=target,
            source_dimension=source_dimension,
            raw_records_from_payload=_fmp_company_profile_records_from_payload,
        )
        if not raw_records:
            raise ValueError(f"FMP profile-cik returned no records for CIK {target.cik}")
        return source_document_from_raw_record(raw_records[0], source_strength=source_strength, ttl_hours=ttl_hours)

    def search_company_name(
        self,
        query: str,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
        limit: int = 10,
        exchange: str | None = None,
    ) -> list[SourceDocument]:
        """Search FMP's company-name index for ticker/CIK identity candidates."""

        params = {"query": query, "limit": limit, "exchange": exchange}
        _payload, raw_records = self._get_json(
            operation="company_name_search",
            url=f"{self.base_url}/search-name",
            params=self._auth_params(params),
            source_dimension=source_dimension,
            raw_records_from_payload=_fmp_company_search_records_from_payload,
        )
        return [
            source_document_from_raw_record(raw_record, source_strength=source_strength, ttl_hours=ttl_hours)
            for raw_record in raw_records
        ]

    def search_mergers_acquisitions(
        self,
        name: str,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
        limit: int = 100,
        page: int = 0,
    ) -> list[SourceDocument]:
        """Search FMP structured M&A records by company name."""

        _payload, raw_records = self._get_json(
            operation="mergers_acquisitions_search",
            url=f"{self.base_url}/mergers-acquisitions-search",
            params=self._auth_params({"name": name, "limit": limit, "page": page}),
            source_dimension=source_dimension,
            raw_records_from_payload=_fmp_mna_records_from_payload,
        )
        return [
            source_document_from_raw_record(raw_record, source_strength=source_strength, ttl_hours=ttl_hours)
            for raw_record in raw_records
        ]

    def _auth_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Attach FMP's documented query-parameter API key and remove empty optional parameters."""

        cleaned = {key: value for key, value in params.items() if value is not None}
        cleaned["apikey"] = self.settings.fmp_api_key
        return cleaned


def _fmp_company_profile_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    return [
        _fmp_raw_record(
            record,
            context,
            retrieved_at,
            source_type=SourceType.exchange_profile,
            record_role="company_profile",
            record_id=_first_text(record, "symbol", "cik", "companyName", "company_name"),
            raw_text=_first_text(record, "description", "companyDescription", "company_description"),
        )
        for record in _payload_records(payload)
    ]


def _fmp_company_search_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    return [
        _fmp_raw_record(
            record,
            context,
            retrieved_at,
            source_type=SourceType.exchange_profile,
            record_role="company_name_search_result",
            record_id=_first_text(record, "symbol", "cik", "name", "companyName"),
        )
        for record in _payload_records(payload)
    ]


def _fmp_mna_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    return [
        _fmp_raw_record(
            record,
            context,
            retrieved_at,
            source_type=SourceType.transaction_signal,
            record_role="mergers_acquisitions_result",
            record_id=_first_text(record, "transactionId", "symbol", "cik", "acquiringCompanyName", "companyName", "url", "link"),
            url=_first_text(record, "url", "link", "filingUrl", "filing_url") or context.url,
            published_at=_first_text(record, "transactionDate", "date", "filingDate", "acceptedDate"),
            raw_text=_fmp_mna_raw_text(record),
        )
        for record in _payload_records(payload)
    ]


def _fmp_raw_record(
    record: dict[str, Any],
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    *,
    source_type: SourceType,
    record_role: str,
    record_id: str | None,
    url: str | None = None,
    published_at: str | None = None,
    raw_text: str | None = None,
) -> DataSourceRawRecord:
    payload = {
        **record,
        "record_role": record_role,
        "provider_endpoint": context.operation,
    }
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=source_type,
        target_cik=context.target_cik,
        target_ticker=context.target_ticker,
        record_id=record_id or context.url,
        url=url or context.url,
        published_at=published_at,
        raw_payload={key: value for key, value in payload.items() if value is not None},
        raw_text=raw_text,
        metadata={"record_role": record_role},
        retrieved_at=retrieved_at,
    )


def _payload_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _first_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _fmp_mna_raw_text(record: dict[str, Any]) -> str | None:
    buyer = _first_text(record, "acquiringCompanyName", "acquirerName", "buyer", "buyerName")
    target = _first_text(record, "acquiredCompanyName", "targetCompanyName", "target", "sellerName")
    date = _first_text(record, "transactionDate", "date", "filingDate")
    parts = []
    if buyer and target:
        parts.append(f"{buyer} acquired {target}.")
    elif buyer:
        parts.append(f"Acquirer: {buyer}.")
    elif target:
        parts.append(f"Target: {target}.")
    if date:
        parts.append(f"Transaction date: {date}.")
    return " ".join(parts) or None
