"""SQLite-backed cache for Phase 2 target profile extraction results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain import TargetProfileExtractionResult
from src.repositories.models import TargetProfileCacheRecord


class TargetProfileCache:
    """Stores complete profile extraction payloads until Phase 3 adds normalized evidence tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_valid(
        self,
        target_cik: str,
        extractor_version: str,
        source_fingerprint: str,
    ) -> TargetProfileExtractionResult | None:
        now = datetime.now(UTC)
        statement = (
            select(TargetProfileCacheRecord)
            .where(TargetProfileCacheRecord.target_cik == target_cik)
            .where(TargetProfileCacheRecord.extractor_version == extractor_version)
            .where(TargetProfileCacheRecord.source_fingerprint == source_fingerprint)
            .order_by(TargetProfileCacheRecord.created_at.desc())
        )
        record = self.session.execute(statement).scalars().first()
        if not record or (record.expires_at and _as_utc(record.expires_at) <= now):
            return None
        return TargetProfileExtractionResult.model_validate_json(record.payload_json)

    def save(
        self,
        target_cik: str,
        target_ticker: str | None,
        extractor_version: str,
        source_fingerprint: str,
        result: TargetProfileExtractionResult,
        ttl_hours: int,
    ) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None
        self.session.add(
            TargetProfileCacheRecord(
                target_cik=target_cik,
                target_ticker=target_ticker,
                extractor_version=extractor_version,
                source_fingerprint=source_fingerprint,
                payload_json=result.model_dump_json(),
                expires_at=expires_at,
            )
        )
        self.session.commit()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
