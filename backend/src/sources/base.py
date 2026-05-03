"""Shared abstractions for external public-data source adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

import httpx

from src.config import Settings
from src.domain import (
    DataSourceRawRecord,
    DataSourceRequest,
    DataSourceRequestStatus,
    ResolvedTarget,
    SourceDocument,
    SourceStrength,
)


class SourceClient(Protocol):
    """Backward-compatible marker for source clients with simple fetch semantics."""

    def fetch(self, identifier: str) -> str:
        ...


class DataSourceRequestRecorder(Protocol):
    """Persistence boundary used by source clients to store request audit records."""

    def record(self, request: DataSourceRequest, raw_records: Sequence[DataSourceRawRecord]) -> None:
        ...


@dataclass(frozen=True)
class DataSourceRequestContext:
    """In-flight external request context shared by source-specific normalizers."""

    request_id: str
    source_id: str
    provider: str
    operation: str
    source_dimension: str | None
    target_cik: str | None
    target_ticker: str | None
    method: str
    url: str | None
    request_params: dict[str, Any]
    request_headers: dict[str, Any]
    request_body: Any | None
    requested_at: datetime
    started_monotonic: float


JsonRecordBuilder = Callable[[Any, DataSourceRequestContext, datetime], list[DataSourceRawRecord]]
TextRecordBuilder = Callable[[str, DataSourceRequestContext, datetime, httpx.Response], list[DataSourceRawRecord]]


class ExternalDataSourceClient:
    """Base class that records every external provider request in one audit shape."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_id: str,
        provider: str,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.source_id = source_id
        self.provider = provider
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds)
        self.request_recorder = request_recorder

    def _get_json(
        self,
        *,
        operation: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        target: ResolvedTarget | None = None,
        source_dimension: str | None = None,
        raw_records_from_payload: JsonRecordBuilder,
    ) -> tuple[Any, list[DataSourceRawRecord]]:
        context = self._start_request(
            operation=operation,
            method="GET",
            url=url,
            params=params,
            headers=headers,
            target=target,
            source_dimension=source_dimension,
        )
        status_code: int | None = None
        try:
            response = self.http_client.get(url, params=params, headers=headers)
            status_code = response.status_code
            response.raise_for_status()
            payload = response.json()
            retrieved_at = datetime.now(UTC)
            raw_records = raw_records_from_payload(payload, context, retrieved_at)
            self._record_request(context, DataSourceRequestStatus.success, response.status_code, raw_records)
            return payload, raw_records
        except Exception as error:
            self._record_request(
                context,
                DataSourceRequestStatus.error,
                status_code or _status_code_from_error(error),
                [],
                error_message=f"{type(error).__name__}: {error}",
            )
            raise

    def _get_text(
        self,
        *,
        operation: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        target: ResolvedTarget | None = None,
        source_dimension: str | None = None,
        raw_records_from_text: TextRecordBuilder,
    ) -> tuple[str, list[DataSourceRawRecord]]:
        context = self._start_request(
            operation=operation,
            method="GET",
            url=url,
            params=params,
            headers=headers,
            target=target,
            source_dimension=source_dimension,
        )
        status_code: int | None = None
        try:
            response = self.http_client.get(url, params=params, headers=headers)
            status_code = response.status_code
            response.raise_for_status()
            text = response.text
            retrieved_at = datetime.now(UTC)
            raw_records = raw_records_from_text(text, context, retrieved_at, response)
            self._record_request(context, DataSourceRequestStatus.success, response.status_code, raw_records)
            return text, raw_records
        except Exception as error:
            self._record_request(
                context,
                DataSourceRequestStatus.error,
                status_code or _status_code_from_error(error),
                [],
                error_message=f"{type(error).__name__}: {error}",
            )
            raise

    def _start_provider_request(
        self,
        *,
        operation: str,
        target: ResolvedTarget | None = None,
        source_dimension: str | None = None,
        request_params: dict[str, Any] | None = None,
    ) -> DataSourceRequestContext:
        return self._start_request(
            operation=operation,
            method="PROVIDER",
            url=None,
            params=request_params,
            headers=None,
            target=target,
            source_dimension=source_dimension,
        )

    def _record_provider_success(
        self,
        context: DataSourceRequestContext,
        raw_records: Sequence[DataSourceRawRecord],
    ) -> None:
        self._record_request(context, DataSourceRequestStatus.success, None, raw_records)

    def _record_provider_error(self, context: DataSourceRequestContext, error: Exception) -> None:
        self._record_request(
            context,
            DataSourceRequestStatus.error,
            None,
            [],
            error_message=f"{type(error).__name__}: {error}",
        )

    def _start_request(
        self,
        *,
        operation: str,
        method: str,
        url: str | None,
        params: dict[str, Any] | None,
        headers: dict[str, Any] | None,
        target: ResolvedTarget | None,
        source_dimension: str | None,
        request_body: Any | None = None,
    ) -> DataSourceRequestContext:
        return DataSourceRequestContext(
            request_id=str(uuid4()),
            source_id=self.source_id,
            provider=self.provider,
            operation=operation,
            source_dimension=source_dimension,
            target_cik=target.cik if target else None,
            target_ticker=target.ticker if target else None,
            method=method,
            url=url,
            request_params=_redact_mapping(params or {}),
            request_headers=_redact_mapping(headers or {}),
            request_body=_redact_value(request_body),
            requested_at=datetime.now(UTC),
            started_monotonic=perf_counter(),
        )

    def _record_request(
        self,
        context: DataSourceRequestContext,
        status: DataSourceRequestStatus,
        status_code: int | None,
        raw_records: Sequence[DataSourceRawRecord],
        error_message: str | None = None,
    ) -> None:
        if not self.request_recorder:
            return

        completed_at = datetime.now(UTC)
        request = DataSourceRequest(
            request_id=context.request_id,
            source_id=context.source_id,
            provider=context.provider,
            operation=context.operation,
            source_dimension=context.source_dimension,
            target_cik=context.target_cik,
            target_ticker=context.target_ticker,
            method=context.method,
            url=context.url,
            request_params=context.request_params,
            request_headers=context.request_headers,
            request_body=context.request_body,
            status=status,
            status_code=status_code,
            error_message=error_message,
            requested_at=context.requested_at,
            completed_at=completed_at,
            duration_ms=max(0, round((perf_counter() - context.started_monotonic) * 1000)),
        )
        self.request_recorder.record(request, raw_records)


def source_document_from_raw_record(
    record: DataSourceRawRecord,
    *,
    source_strength: SourceStrength,
    ttl_hours: int,
    raw_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SourceDocument:
    """Convert one normalized raw record into the existing cached source-document model."""

    return SourceDocument(
        source_id=record.source_id,
        source_dimension=record.source_dimension,
        source_type=record.source_type,
        source_strength=source_strength,
        target_cik=record.target_cik,
        target_ticker=record.target_ticker,
        url=record.url,
        filing_accession=record.filing_accession,
        raw_text=record.raw_text if raw_text is None else raw_text,
        metadata=record.raw_payload if metadata is None else metadata,
        retrieved_at=record.retrieved_at,
        expires_at=record.retrieved_at + timedelta(hours=ttl_hours) if ttl_hours else None,
    )


def _status_code_from_error(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_value(value, key) for key, value in values.items()}


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key and _is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {child_key: _redact_value(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").casefold()
    return normalized in {"apikey", "api_key", "authorization", "token", "secret"} or normalized.endswith("_token")
