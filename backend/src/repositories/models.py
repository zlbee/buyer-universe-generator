"""SQLAlchemy tables used by the Phase 0 persistence foundation."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.repositories.database import Base


class PipelineRunRecord(Base):
    """Durable run envelope; detailed pipeline tables are added in later phases."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    input_query: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="created")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TargetResolutionRecord(Base):
    """Cached target identity resolution keyed by the original user query."""

    __tablename__ = "target_resolutions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    input_query: Mapped[str] = mapped_column(String(256), index=True)
    canonical_name: Mapped[str] = mapped_column(String(256))
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    cik: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_confidence: Mapped[str] = mapped_column(String(16))
    source_provenance_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceDocumentRecord(Base):
    """Cached source document metadata and optional raw text for a target."""

    __tablename__ = "source_documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_dimension: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_strength: Mapped[str] = mapped_column(String(4))
    target_cik: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    target_ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_accession: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DataSourceRequestRecord(Base):
    """External raw data-source request audit trail for later provider inspection."""

    __tablename__ = "data_source_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(128), index=True)
    operation: Mapped[str] = mapped_column(String(128), index=True)
    source_dimension: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    target_cik: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    target_ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    method: Mapped[str] = mapped_column(String(16))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_params_json: Mapped[str] = mapped_column(Text, default="{}")
    request_headers_json: Mapped[str] = mapped_column(Text, default="{}")
    request_body_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DataSourceRawRecordRecord(Base):
    """Provider-neutral raw records returned by audited external data-source requests."""

    __tablename__ = "data_source_raw_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("data_source_requests.request_id"),
        index=True,
    )
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(128), index=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_dimension: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    target_cik: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    target_ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    record_id: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_accession: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class TargetProfileCacheRecord(Base):
    """Cached Phase 2 target profile keyed by source fingerprint and extractor version."""

    __tablename__ = "target_profile_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_cik: Mapped[str] = mapped_column(String(32), index=True)
    target_ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    extractor_version: Mapped[str] = mapped_column(String(64), index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanyFinancialMetricsCacheRecord(Base):
    """Cached normalized public-company metrics keyed by source fingerprint."""

    __tablename__ = "company_financial_metrics_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cik: Mapped[str] = mapped_column(String(32), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    source_fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMInteractionRecord(Base):
    """Provider-level LLM request/response audit trail for prompt review and optimization."""

    __tablename__ = "llm_interactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_business_type: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(256), index=True)
    schema_name: Mapped[str] = mapped_column(String(128), index=True)
    response_format_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload_json: Mapped[str] = mapped_column(Text)
    response_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
