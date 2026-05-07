"""Materialize normalized source records into cacheable source documents."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from src.domain import DataSourceRawRecord, SourceDocument, SourceStrength


def source_document_from_raw_record(
    record: DataSourceRawRecord,
    *,
    source_strength: SourceStrength,
    ttl_hours: int,
    raw_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SourceDocument:
    """Convert one normalized raw record into the cached source-document model."""

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
