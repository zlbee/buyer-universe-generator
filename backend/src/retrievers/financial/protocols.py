"""Source boundary protocols for financial buyer retrievers."""

from __future__ import annotations

from typing import Protocol

from src.domain import SourceDocument, SourceStrength


class PEDealActivitySource(Protocol):
    """Source boundary for structured PE deal activity search."""

    def search_mergers_acquisitions(
        self,
        name: str,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
        limit: int = 100,
        page: int = 0,
    ) -> list[SourceDocument]:
        ...
