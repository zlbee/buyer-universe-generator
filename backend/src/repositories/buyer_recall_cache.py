"""SQLite-backed cache for seller-scoped buyer recall stage outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain import StrategicRetrievalResult, TargetProfile
from src.repositories.models import BuyerRecallCacheRecord


_RECALL_INPUT_FIELDS = (
    "target_id",
    "name",
    "ticker",
    "cik",
    "exchange",
    "sic",
    "business_summary",
    "company_strategy",
    "products",
    "customer_segments",
    "channels",
    "geographies",
    "size_metrics",
    "keywords",
    "keyword_groups",
    "adjacent_categories",
    "feature_labels",
)


@dataclass(frozen=True)
class BuyerRecallCacheHit:
    """A valid cached stage result plus metadata for downstream observability."""

    result: StrategicRetrievalResult
    metadata: dict[str, Any]


class BuyerRecallCache:
    """Stores complete Potential Buyer Recaller stage payloads for later pipeline steps."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_valid(
        self,
        target_profile: TargetProfile,
        stage_name: str,
        stage_version: str,
    ) -> BuyerRecallCacheHit | None:
        now = datetime.now(UTC)
        fingerprint = target_profile_fingerprint(target_profile)
        statement = (
            select(BuyerRecallCacheRecord)
            .where(BuyerRecallCacheRecord.target_cik == target_profile.cik)
            .where(BuyerRecallCacheRecord.stage_name == stage_name)
            .where(BuyerRecallCacheRecord.stage_version == stage_version)
            .where(BuyerRecallCacheRecord.target_profile_fingerprint == fingerprint)
            .order_by(BuyerRecallCacheRecord.created_at.desc())
        )
        records = self.session.execute(statement).scalars().all()
        for record in records:
            if record.expires_at and _as_utc(record.expires_at) <= now:
                continue
            return BuyerRecallCacheHit(
                result=StrategicRetrievalResult.model_validate_json(record.payload_json),
                metadata=_cache_metadata(
                    "hit",
                    target_profile=target_profile,
                    stage_name=stage_name,
                    stage_version=stage_version,
                    target_profile_fingerprint=fingerprint,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                ),
            )
        return None

    def save(
        self,
        target_profile: TargetProfile,
        stage_name: str,
        stage_version: str,
        result: StrategicRetrievalResult,
        ttl_hours: int,
    ) -> dict[str, Any]:
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(hours=ttl_hours) if ttl_hours else None
        fingerprint = target_profile_fingerprint(target_profile)
        self.session.add(
            BuyerRecallCacheRecord(
                target_cik=target_profile.cik,
                target_ticker=target_profile.ticker,
                stage_name=stage_name,
                stage_version=stage_version,
                target_profile_fingerprint=fingerprint,
                payload_json=result.model_dump_json(),
                created_at=created_at,
                expires_at=expires_at,
            )
        )
        self.session.commit()
        return _cache_metadata(
            "miss",
            target_profile=target_profile,
            stage_name=stage_name,
            stage_version=stage_version,
            target_profile_fingerprint=fingerprint,
            created_at=created_at,
            expires_at=expires_at,
            ttl_hours=ttl_hours,
        )


def target_profile_fingerprint(target_profile: TargetProfile) -> str:
    """Return a stable fingerprint so changed seller profiles do not reuse stale recall."""

    # Evidence timestamps can change between equivalent profile builds, so the
    # cache key uses the seller features that drive recall rather than citations.
    profile_payload = target_profile.model_dump(mode="json")
    cache_input = {field: profile_payload.get(field) for field in _RECALL_INPUT_FIELDS}
    payload = json.dumps(cache_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_metadata(
    status: str,
    *,
    target_profile: TargetProfile,
    stage_name: str,
    stage_version: str,
    target_profile_fingerprint: str,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "status": status,
        "stage_name": stage_name,
        "stage_version": stage_version,
        "target_cik": target_profile.cik,
        "target_ticker": target_profile.ticker,
        "target_profile_fingerprint": target_profile_fingerprint,
    }
    if ttl_hours is not None:
        metadata["ttl_hours"] = ttl_hours
    if created_at:
        metadata["created_at"] = _as_utc(created_at).isoformat()
    if expires_at:
        metadata["expires_at"] = _as_utc(expires_at).isoformat()
    return metadata


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
