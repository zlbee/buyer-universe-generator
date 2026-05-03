"""SQLite-backed audit log for external raw data-source requests and records."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from src.domain import DataSourceRawRecord, DataSourceRequest
from src.repositories.models import DataSourceRawRecordRecord, DataSourceRequestRecord


class DataSourceAuditLog:
    """Persists provider requests and normalized raw records for later inspection."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, request: DataSourceRequest, raw_records: Sequence[DataSourceRawRecord]) -> None:
        self.session.add(
            DataSourceRequestRecord(
                request_id=request.request_id,
                source_id=request.source_id,
                provider=request.provider,
                operation=request.operation,
                source_dimension=request.source_dimension,
                target_cik=request.target_cik,
                target_ticker=request.target_ticker,
                method=request.method,
                url=request.url,
                request_params_json=_dump_json(request.request_params),
                request_headers_json=_dump_json(request.request_headers),
                request_body_json=_dump_json(request.request_body) if request.request_body is not None else None,
                status=request.status.value,
                status_code=request.status_code,
                error_message=request.error_message,
                requested_at=request.requested_at,
                completed_at=request.completed_at,
                duration_ms=request.duration_ms,
            )
        )
        for raw_record in raw_records:
            self.session.add(
                DataSourceRawRecordRecord(
                    request_id=raw_record.request_id,
                    source_id=raw_record.source_id,
                    provider=raw_record.provider,
                    source_type=raw_record.source_type.value,
                    source_dimension=raw_record.source_dimension,
                    target_cik=raw_record.target_cik,
                    target_ticker=raw_record.target_ticker,
                    record_id=raw_record.record_id,
                    url=raw_record.url,
                    filing_accession=raw_record.filing_accession,
                    title=raw_record.title,
                    published_at=raw_record.published_at,
                    raw_payload_json=_dump_json(raw_record.raw_payload),
                    raw_text=raw_record.raw_text,
                    metadata_json=_dump_json(raw_record.metadata),
                    retrieved_at=raw_record.retrieved_at,
                )
            )
        self.session.commit()


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
