"""Source boundary protocols for strategic buyer retrievers."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from src.domain import ResolvedTarget, SourceDocument, SourceStrength, TargetProfile


class PublicCompanySicSource(Protocol):
    """Source boundary for public-company same-SIC profile discovery."""

    def list_public_company_profiles_by_sic(
        self,
        sic: str,
        limit: int,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        ...


class TransactionNewsSource(Protocol):
    """Source boundary for transaction-news search."""

    def fetch_articles_for_query(
        self,
        query: str,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 10,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
        domains: list[str] | None = None,
        from_date: str | None = None,
    ) -> list[SourceDocument]:
        ...


class TransactionFilingSource(Protocol):
    """Source boundary for SEC transaction-signal retrieval from same-industry 8-K filings."""

    def fetch_transaction_signal_documents(
        self,
        target_profile: TargetProfile,
        since: date,
        ttl_hours: int,
        form_type: str,
        limit: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        ...
