"""Source abstraction for acquirer-capable universe seed and metrics retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from src.domain import (
    AcquirerEntity,
    AcquirerListingStatus,
    CompanyFinancialMetrics,
    ResolvedTarget,
    SourceStrength,
)
from src.repositories.financial_metrics_cache import CompanyFinancialMetricsCache
from src.sources.sec import SecEdgarClient


SEC_METRICS_FINGERPRINT = "sec-companyfacts-financial-metrics-v1"


class AcquirerUniverseSource(Protocol):
    """Boundary for seed universes that can feed acquirer-capability screening."""

    source_id: str
    source_fingerprint: str

    def list_seed_entities(self) -> list[AcquirerEntity]:
        ...

    def fetch_metrics_bulk(
        self,
        entities: Sequence[AcquirerEntity],
        source_strength: SourceStrength,
        source_dimension: str | None,
        warnings: list[str],
    ) -> dict[str, CompanyFinancialMetrics]:
        ...

    def fetch_metrics(
        self,
        entity: AcquirerEntity,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> CompanyFinancialMetrics:
        ...


@dataclass
class SecListedCompanyUniverseSource:
    """SEC-listed operating-company source used by Acquirer-Capable Universe v1."""

    edgar_client: SecEdgarClient
    metrics_cache: CompanyFinancialMetricsCache
    cache_ttl_hours: int
    source_id: str = "edgar"
    source_fingerprint: str = SEC_METRICS_FINGERPRINT

    def list_seed_entities(self) -> list[AcquirerEntity]:
        return [
            acquirer_entity_from_resolved_target(target, source_id=self.source_id)
            for target in self.edgar_client.fetch_public_company_universe()
        ]

    def fetch_metrics_bulk(
        self,
        entities: Sequence[AcquirerEntity],
        source_strength: SourceStrength,
        source_dimension: str | None,
        warnings: list[str],
    ) -> dict[str, CompanyFinancialMetrics]:
        cached_metrics: dict[str, CompanyFinancialMetrics] = {}
        uncached_targets: list[ResolvedTarget] = []
        target_entity_ids: dict[str, str] = {}
        for entity in entities:
            target = resolved_target_from_acquirer_entity(entity)
            cached = self.metrics_cache.get_valid(target.cik, self.source_fingerprint)
            if cached:
                cached_metrics[entity.entity_id] = cached
                continue
            uncached_targets.append(target)
            target_entity_ids[target.cik] = entity.entity_id

        bulk_fetch = getattr(self.edgar_client, "fetch_company_financial_metrics_bulk", None)
        if not callable(bulk_fetch) or not uncached_targets:
            return cached_metrics

        try:
            fetched_metrics = bulk_fetch(
                uncached_targets,
                source_strength=source_strength,
                source_dimension=source_dimension,
            )
        except Exception as error:
            warnings.append(
                "SEC bulk companyfacts metrics failed; "
                f"falling back to per-company fetches: {type(error).__name__}: {error}"
            )
            return cached_metrics

        for cik, metrics in fetched_metrics.items():
            self.metrics_cache.save(metrics, self.source_fingerprint, self.cache_ttl_hours)
            entity_id = target_entity_ids.get(cik, cik)
            cached_metrics[entity_id] = metrics
        return cached_metrics

    def fetch_metrics(
        self,
        entity: AcquirerEntity,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> CompanyFinancialMetrics:
        target = resolved_target_from_acquirer_entity(entity)
        cached = self.metrics_cache.get_valid(target.cik, self.source_fingerprint)
        if cached:
            return cached

        metrics = self.edgar_client.fetch_company_financial_metrics(
            target,
            source_strength=source_strength,
            source_dimension=source_dimension,
        )
        self.metrics_cache.save(metrics, self.source_fingerprint, self.cache_ttl_hours)
        return metrics


def acquirer_entity_from_resolved_target(target: ResolvedTarget, source_id: str) -> AcquirerEntity:
    return AcquirerEntity(
        source_id=source_id,
        entity_id=target.cik,
        entity_id_type="cik",
        canonical_name=target.canonical_name,
        ticker=target.ticker,
        cik=target.cik,
        exchange=target.exchange,
        sic=target.sic,
        listing_status=AcquirerListingStatus.public,
        source_provenance=target.source_provenance,
    )


def resolved_target_from_acquirer_entity(entity: AcquirerEntity) -> ResolvedTarget:
    if not entity.ticker or not entity.cik:
        raise ValueError(f"{entity.source_id} entity {entity.entity_id} lacks ticker or CIK for public-company v1 screening")
    return ResolvedTarget(
        canonical_name=entity.canonical_name,
        ticker=entity.ticker,
        cik=entity.cik,
        exchange=entity.exchange,
        sic=entity.sic,
        resolution_confidence=1.0,
        matched_input=entity.ticker,
        source_provenance=entity.source_provenance or [f"{entity.source_id}:{entity.entity_id_type}"],
    )
