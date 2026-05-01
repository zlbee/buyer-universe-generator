"""SQLAlchemy tables used by the Phase 0 persistence foundation."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
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
