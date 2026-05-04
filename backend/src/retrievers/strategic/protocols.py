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


class TransactionRssNewsSource(Protocol):
    """Source boundary for RSS-based transaction-news search and topic feeds."""

    def fetch_articles_for_query(
        self,
        query: str,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 10,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        ...

    def fetch_topic_articles(
        self,
        topic: str,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 10,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        ...


class BuyerIdentityResolver(Protocol):
    """Source boundary for resolving parsed buyer names to public-company identity."""

    def resolve_by_company_name(self, company_name: str) -> list[ResolvedTarget]:
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
        company_limit: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
        primary_items: list[str] | None = None,
        supporting_items: list[str] | None = None,
        require_primary_item: bool = False,
        fetch_filing_text: bool = False,
        text_scope: str | None = None,
        as_of_date: date | None = None,
    ) -> list[SourceDocument]:
        ...
