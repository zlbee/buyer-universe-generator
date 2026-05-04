"""SQLite-backed cache for normalized company financial metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain import CompanyFinancialMetrics
from src.repositories.models import CompanyFinancialMetricsCacheRecord


class CompanyFinancialMetricsCache:
    """Caches SEC-derived financial metrics so universe screens can be resumed cheaply."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_valid(self, cik: str, source_fingerprint: str) -> CompanyFinancialMetrics | None:
        now = datetime.now(UTC)
        statement = (
            select(CompanyFinancialMetricsCacheRecord)
            .where(CompanyFinancialMetricsCacheRecord.cik == cik)
            .where(CompanyFinancialMetricsCacheRecord.source_fingerprint == source_fingerprint)
            .order_by(CompanyFinancialMetricsCacheRecord.created_at.desc())
        )
        record = self.session.execute(statement).scalars().first()
        if not record or (record.expires_at and _as_utc(record.expires_at) <= now):
            return None
        return CompanyFinancialMetrics.model_validate_json(record.payload_json)

    def find_valid_by_query(self, query: str, source_fingerprint: str) -> CompanyFinancialMetrics | None:
        return find_metrics_by_query(self.list_valid(source_fingerprint), query)

    def list_valid(self, source_fingerprint: str) -> list[CompanyFinancialMetrics]:
        now = datetime.now(UTC)
        statement = (
            select(CompanyFinancialMetricsCacheRecord)
            .where(CompanyFinancialMetricsCacheRecord.source_fingerprint == source_fingerprint)
            .order_by(CompanyFinancialMetricsCacheRecord.created_at.desc())
        )
        records = self.session.execute(statement).scalars().all()

        deduped: dict[str, CompanyFinancialMetrics] = {}
        for record in records:
            if record.expires_at and _as_utc(record.expires_at) <= now:
                continue
            if record.cik in deduped:
                continue
            deduped[record.cik] = CompanyFinancialMetrics.model_validate_json(record.payload_json)
        return list(deduped.values())

    def save(self, metrics: CompanyFinancialMetrics, source_fingerprint: str, ttl_hours: int) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None
        self.session.add(
            CompanyFinancialMetricsCacheRecord(
                cik=metrics.cik,
                ticker=metrics.ticker,
                source_fingerprint=source_fingerprint,
                payload_json=metrics.model_dump_json(),
                expires_at=expires_at,
            )
        )
        self.session.commit()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def find_metrics_by_query(metrics_list: list[CompanyFinancialMetrics], query: str) -> CompanyFinancialMetrics | None:
    """Resolve a user query against an already-loaded metrics list without another DB scan."""

    normalized = query.strip()
    if not normalized:
        return None

    normalized_upper = normalized.upper()
    normalized_cik = normalized.zfill(10) if normalized.isdigit() else normalized
    for metrics in metrics_list:
        if metrics.ticker.upper() == normalized_upper or metrics.cik == normalized_cik:
            return metrics
        if metrics.canonical_name.casefold() == normalized.casefold():
            return metrics
    return None
