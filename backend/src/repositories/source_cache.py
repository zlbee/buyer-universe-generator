"""SQLite-backed cache for target resolution and source ingestion metadata."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain import ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.repositories.models import SourceDocumentRecord, TargetResolutionRecord


class SourceCache:
    """Caches Phase 1 source data so repeated runs avoid unnecessary API calls."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_valid_resolution(self, input_query: str) -> ResolvedTarget | None:
        now = datetime.now(UTC)
        statement = (
            select(TargetResolutionRecord)
            .where(TargetResolutionRecord.input_query == input_query.strip())
            .order_by(TargetResolutionRecord.created_at.desc())
        )
        record = self.session.execute(statement).scalars().first()
        if not record or (record.expires_at and _as_utc(record.expires_at) <= now):
            return None

        return ResolvedTarget(
            canonical_name=record.canonical_name,
            ticker=record.ticker,
            cik=record.cik,
            exchange=record.exchange,
            sic=record.sic,
            resolution_confidence=float(record.resolution_confidence),
            matched_input=record.input_query,
            source_provenance=json.loads(record.source_provenance_json),
        )

    def save_resolution(self, input_query: str, target: ResolvedTarget, ttl_hours: int) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None
        self.session.add(
            TargetResolutionRecord(
                input_query=input_query.strip(),
                canonical_name=target.canonical_name,
                ticker=target.ticker,
                cik=target.cik,
                exchange=target.exchange,
                sic=target.sic,
                resolution_confidence=f"{target.resolution_confidence:.4f}",
                source_provenance_json=json.dumps(target.source_provenance),
                expires_at=expires_at,
            )
        )
        self.session.commit()

    def get_valid_documents(self, target_cik: str, source_id: str | None = None) -> list[SourceDocument]:
        now = datetime.now(UTC)
        statement = select(SourceDocumentRecord).where(SourceDocumentRecord.target_cik == target_cik)
        if source_id:
            statement = statement.where(SourceDocumentRecord.source_id == source_id)

        records = self.session.execute(statement).scalars().all()
        return [self._document_from_record(record) for record in records if not record.expires_at or _as_utc(record.expires_at) > now]

    def save_document(self, document: SourceDocument) -> None:
        self.session.add(
            SourceDocumentRecord(
                source_id=document.source_id,
                source_dimension=document.source_dimension,
                source_type=document.source_type.value,
                source_strength=document.source_strength.value,
                target_cik=document.target_cik,
                target_ticker=document.target_ticker,
                url=document.url,
                filing_accession=document.filing_accession,
                raw_text=document.raw_text,
                metadata_json=json.dumps(document.metadata),
                retrieved_at=document.retrieved_at,
                expires_at=document.expires_at,
            )
        )
        self.session.commit()

    def save_documents(self, documents: list[SourceDocument]) -> None:
        for document in documents:
            self.session.add(
                SourceDocumentRecord(
                    source_id=document.source_id,
                    source_dimension=document.source_dimension,
                    source_type=document.source_type.value,
                    source_strength=document.source_strength.value,
                    target_cik=document.target_cik,
                    target_ticker=document.target_ticker,
                    url=document.url,
                    filing_accession=document.filing_accession,
                    raw_text=document.raw_text,
                    metadata_json=json.dumps(document.metadata),
                    retrieved_at=document.retrieved_at,
                    expires_at=document.expires_at,
                )
            )
        self.session.commit()

    def _document_from_record(self, record: SourceDocumentRecord) -> SourceDocument:
        return SourceDocument(
            source_id=record.source_id,
            source_dimension=record.source_dimension,
            source_type=SourceType(record.source_type),
            source_strength=SourceStrength(record.source_strength),
            target_cik=record.target_cik,
            target_ticker=record.target_ticker,
            url=record.url,
            filing_accession=record.filing_accession,
            raw_text=record.raw_text,
            metadata=json.loads(record.metadata_json),
            retrieved_at=record.retrieved_at,
            expires_at=record.expires_at,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
