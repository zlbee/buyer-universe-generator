"""Target resolution service for Phase 1."""

from __future__ import annotations

from dataclasses import dataclass

from src.domain import ResolvedTarget
from src.repositories.source_cache import SourceCache
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy


class TargetResolutionError(Exception):
    """Base class for expected target-resolution failures."""


class TargetNotFoundError(TargetResolutionError):
    """Raised when no configured source can resolve the input."""


@dataclass
class AmbiguousTargetError(TargetResolutionError):
    """Raised when an exact company-name lookup produces multiple candidates."""

    message: str
    candidates: list[ResolvedTarget]

    def __str__(self) -> str:
        return self.message


class TargetResolver:
    """Resolves a ticker, CIK, or exact company name into canonical target identity."""

    def __init__(
        self,
        strategy: DataSourceStrategy,
        edgar_client: SecEdgarClient,
        source_cache: SourceCache | None = None,
    ) -> None:
        self.strategy = strategy
        self.edgar_client = edgar_client
        self.source_cache = source_cache

    def resolve(self, query: str) -> ResolvedTarget:
        normalized_query = query.strip()
        if not normalized_query:
            raise TargetNotFoundError("Target query must not be empty")

        if self.source_cache:
            cached = self.source_cache.get_valid_resolution(normalized_query)
            if cached:
                return cached

        edgar_selection = next((source for source in self.strategy.select("target_identity") if source.source_id == "edgar"), None)
        if not edgar_selection or not edgar_selection.enabled:
            raise TargetNotFoundError("SEC/EDGAR source is required for target resolution but is not enabled")

        target = self._resolve_with_edgar(normalized_query)
        if not target:
            raise TargetNotFoundError(f"Could not resolve target: {query}")

        if self.source_cache:
            self.source_cache.save_resolution(
                input_query=normalized_query,
                target=target,
                ttl_hours=self.strategy.cache_ttl_hours("edgar"),
            )

        return target

    def _resolve_with_edgar(self, query: str) -> ResolvedTarget | None:
        if query.isdigit():
            return self.edgar_client.resolve_by_cik(query)

        ticker_match = self.edgar_client.resolve_by_ticker(query)
        if ticker_match:
            return ticker_match

        name_matches = self.edgar_client.resolve_by_company_name(query)
        if len(name_matches) > 1:
            raise AmbiguousTargetError(
                message=f"Company name matched multiple targets: {query}",
                candidates=name_matches,
            )
        if len(name_matches) == 1:
            return name_matches[0]
        return None
