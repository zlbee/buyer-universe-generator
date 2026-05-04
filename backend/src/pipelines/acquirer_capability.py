"""Acquirer-capable public-company universe builder."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json

from src.config import Settings
from src.domain import (
    AcquirerEntity,
    AcquirerCapabilityCandidate,
    AcquirerCapabilityUniverseResult,
    AcquirerCapacityRuleResult,
    AcquirerCapacityRuleStatus,
    CompanyFinancialMetrics,
    Evidence,
    ResolvedTarget,
    SourceDocument,
)
from src.pipelines.acquirer_universe_sources import (
    AcquirerUniverseSource,
    SEC_METRICS_FINGERPRINT,
    SecListedCompanyUniverseSource,
    resolved_target_from_acquirer_entity,
)
from src.pipelines.target_resolution import TargetResolver
from src.repositories.financial_metrics_cache import CompanyFinancialMetricsCache, find_metrics_by_query
from src.repositories.source_cache import SourceCache
from src.sources.polygon import PolygonClient
from src.sources.sec import SecEdgarClient, is_main_us_operating_company_name
from src.sources.strategy import DataSourceStrategy, SelectedDataSource


class AcquirerCapabilityError(RuntimeError):
    """Structured failure for the acquirer-capability universe builder."""

    def __init__(self, message: str, error_code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code


class AcquirerCapabilityUniverseBuilder:
    """Screens public companies for minimum capacity to acquire a target."""

    def __init__(
        self,
        settings: Settings,
        strategy: DataSourceStrategy,
        resolver: TargetResolver,
        source_cache: SourceCache,
        metrics_cache: CompanyFinancialMetricsCache,
        edgar_client: SecEdgarClient,
        polygon_client: PolygonClient | None = None,
        universe_sources: list[AcquirerUniverseSource] | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.resolver = resolver
        self.source_cache = source_cache
        self.metrics_cache = metrics_cache
        self.edgar_client = edgar_client
        self.polygon_client = polygon_client
        self.universe_sources = universe_sources

    def build_universe(self, query: str) -> AcquirerCapabilityUniverseResult:
        if self.settings.acquirer_cache_only_mode:
            return self._build_universe_from_cache(query)

        edgar_source = self.strategy.selected_source("buyer_recall_acquirer_capacity", "edgar", include_disabled=True)
        if not edgar_source or not edgar_source.enabled:
            reason = edgar_source.disabled_reason if edgar_source else "not configured"
            raise AcquirerCapabilityError(
                f"SEC/EDGAR acquirer-capacity source is required but unavailable: {reason}",
                error_code="edgar_acquirer_capacity_unavailable",
                status_code=400,
            )

        polygon_source = self.strategy.selected_source("buyer_recall_acquirer_capacity", "polygon", include_disabled=True)
        warnings: list[str] = []
        if not polygon_source:
            warnings.append("polygon acquirer-capacity screening not configured")
        elif not polygon_source.enabled:
            warnings.append(f"polygon disabled: {polygon_source.disabled_reason}")
        elif not self.polygon_client:
            warnings.append("polygon disabled: adapter unavailable")

        target = self.resolver.resolve(query)
        target_metrics = self._sec_metrics(target, edgar_source)
        target_metrics = self._with_polygon_market_cap(target, target_metrics, polygon_source, warnings)
        thresholds = _thresholds(target_metrics, self.settings)
        warnings.extend(_target_metric_warnings(target_metrics))

        excluded_counts: Counter[str] = Counter()
        coverage: Counter[str] = Counter()
        candidates: list[AcquirerCapabilityCandidate] = []

        seed_entities: list[tuple[AcquirerUniverseSource, AcquirerEntity]] = []
        bulk_metrics: dict[tuple[str, str], CompanyFinancialMetrics] = {}
        for universe_source in self._active_universe_sources():
            source_entities = universe_source.list_seed_entities()
            coverage[f"{universe_source.source_id}_seed_companies"] += len(source_entities)
            coverage["seed_companies"] += len(source_entities)
            seed_entities.extend((universe_source, entity) for entity in source_entities)
            fetched_metrics = universe_source.fetch_metrics_bulk(
                source_entities,
                source_strength=edgar_source.source_strength,
                source_dimension=edgar_source.dimension_id,
                warnings=warnings,
            )
            bulk_metrics.update(
                {
                    (universe_source.source_id, entity_id): metrics
                    for entity_id, metrics in fetched_metrics.items()
                }
            )

        for universe_source, candidate_entity in seed_entities:
            if _same_entity_as_target(candidate_entity, target):
                excluded_counts["target_self"] += 1
                continue
            if not _is_supported_exchange(candidate_entity.exchange):
                excluded_counts["unsupported_exchange"] += 1
                continue
            if not is_main_us_operating_company_name(candidate_entity.canonical_name):
                excluded_counts["non_operating_security"] += 1
                continue

            coverage["screened_companies"] += 1
            try:
                candidate_target = resolved_target_from_acquirer_entity(candidate_entity)
                metrics = bulk_metrics.get((universe_source.source_id, candidate_entity.entity_id)) or universe_source.fetch_metrics(
                    candidate_entity,
                    source_strength=edgar_source.source_strength,
                    source_dimension=edgar_source.dimension_id,
                )
            except Exception as error:
                excluded_counts["financial_metrics_fetch_error"] += 1
                _append_limited_warning(
                    warnings,
                    "acquirer source metrics failed for "
                    f"{candidate_entity.ticker or candidate_entity.entity_id}: {type(error).__name__}: {error}",
                )
                continue

            metrics = self._with_polygon_market_cap(candidate_target, metrics, polygon_source, warnings)
            if metrics.revenue_usd is not None:
                coverage["sec_revenue_available"] += 1
            if metrics.cash_and_equivalents_usd is not None:
                coverage["sec_cash_available"] += 1
            if metrics.market_cap_usd is not None:
                coverage["polygon_market_cap_available"] += 1
            if not _has_any_capacity_metric(metrics):
                excluded_counts["metrics_unavailable"] += 1
                continue

            rule_results = evaluate_capacity_rules(metrics, target_metrics, self.settings)
            passed_rules = [result.rule_id for result in rule_results if result.status == AcquirerCapacityRuleStatus.passed]
            if not passed_rules:
                excluded_counts["capacity_failed"] += 1
                continue

            risk_flags = _risk_flags(metrics, passed_rules)
            candidates.append(
                AcquirerCapabilityCandidate(
                    canonical_name=metrics.canonical_name,
                    ticker=metrics.ticker,
                    cik=metrics.cik,
                    exchange=metrics.exchange,
                    sic=metrics.sic,
                    metrics=metrics,
                    rule_results=rule_results,
                    passed_rules=passed_rules,
                    risk_flags=risk_flags,
                    evidence=metrics.evidence,
                )
            )

        candidates.sort(key=lambda candidate: (_best_rule_ratio(candidate), candidate.metrics.market_cap_usd or 0), reverse=True)
        coverage["candidate_count"] = len(candidates)
        coverage["polygon_enabled"] = bool(polygon_source and polygon_source.enabled and self.polygon_client)

        return AcquirerCapabilityUniverseResult(
            target=target,
            target_metrics=target_metrics,
            thresholds=thresholds,
            candidates=candidates,
            excluded_counts=dict(excluded_counts),
            source_coverage=dict(coverage),
            warnings=warnings,
            generated_at=datetime.now(UTC),
        )

    def _active_universe_sources(self) -> list[AcquirerUniverseSource]:
        if self.universe_sources is not None:
            return self.universe_sources
        return [
            SecListedCompanyUniverseSource(
                edgar_client=self.edgar_client,
                metrics_cache=self.metrics_cache,
                cache_ttl_hours=self.strategy.cache_ttl_hours("edgar"),
            )
        ]

    def _build_universe_from_cache(self, query: str) -> AcquirerCapabilityUniverseResult:
        cached_metrics = self.metrics_cache.list_valid(SEC_METRICS_FINGERPRINT)
        target_metrics = find_metrics_by_query(cached_metrics, query)
        if not target_metrics:
            raise AcquirerCapabilityError(
                "Acquirer cache-only mode is enabled, but the target is not present in company_financial_metrics_cache.",
                error_code="acquirer_cache_only_target_not_cached",
                status_code=404,
            )

        cached_polygon_documents = self.source_cache.get_valid_documents_by_cik(
            [metrics.cik for metrics in cached_metrics],
            source_id="polygon",
        )
        target_metrics = self._with_cached_polygon_market_cap(
            target_metrics,
            cached_polygon_documents.get(target_metrics.cik, []),
        )
        target = _target_from_metrics(target_metrics, matched_input=query)
        thresholds = _thresholds(target_metrics, self.settings)
        warnings = [
            "acquirer cache-only mode enabled: no new SEC or Polygon data will be requested",
            *_target_metric_warnings(target_metrics),
        ]
        excluded_counts: Counter[str] = Counter()
        coverage: Counter[str] = Counter(
            {
                "cache_only_mode": 1,
                "cached_metric_companies": len(cached_metrics),
                "seed_companies": len(cached_metrics),
            }
        )
        candidates: list[AcquirerCapabilityCandidate] = []

        for metrics in cached_metrics:
            if metrics.cik == target_metrics.cik or metrics.ticker.upper() == target_metrics.ticker.upper():
                excluded_counts["target_self"] += 1
                continue
            if not _is_supported_exchange(metrics.exchange):
                excluded_counts["unsupported_exchange"] += 1
                continue
            if not is_main_us_operating_company_name(metrics.canonical_name):
                excluded_counts["non_operating_security"] += 1
                continue

            coverage["screened_companies"] += 1
            hydrated_metrics = self._with_cached_polygon_market_cap(metrics, cached_polygon_documents.get(metrics.cik, []))
            if hydrated_metrics.revenue_usd is not None:
                coverage["sec_revenue_available"] += 1
            if hydrated_metrics.cash_and_equivalents_usd is not None:
                coverage["sec_cash_available"] += 1
            if hydrated_metrics.market_cap_usd is not None:
                coverage["cached_polygon_market_cap_available"] += 1
            if not _has_any_capacity_metric(hydrated_metrics):
                excluded_counts["metrics_unavailable"] += 1
                continue

            rule_results = evaluate_capacity_rules(hydrated_metrics, target_metrics, self.settings)
            passed_rules = [result.rule_id for result in rule_results if result.status == AcquirerCapacityRuleStatus.passed]
            if not passed_rules:
                excluded_counts["capacity_failed"] += 1
                continue

            candidates.append(
                AcquirerCapabilityCandidate(
                    canonical_name=hydrated_metrics.canonical_name,
                    ticker=hydrated_metrics.ticker,
                    cik=hydrated_metrics.cik,
                    exchange=hydrated_metrics.exchange,
                    sic=hydrated_metrics.sic,
                    metrics=hydrated_metrics,
                    rule_results=rule_results,
                    passed_rules=passed_rules,
                    risk_flags=_risk_flags(hydrated_metrics, passed_rules),
                    evidence=hydrated_metrics.evidence,
                )
            )

        candidates.sort(key=lambda candidate: (_best_rule_ratio(candidate), candidate.metrics.market_cap_usd or 0), reverse=True)
        coverage["candidate_count"] = len(candidates)
        return AcquirerCapabilityUniverseResult(
            target=target,
            target_metrics=target_metrics,
            thresholds=thresholds,
            candidates=candidates,
            excluded_counts=dict(excluded_counts),
            source_coverage=dict(coverage),
            warnings=warnings,
            generated_at=datetime.now(UTC),
        )

    def _sec_metrics(self, target: ResolvedTarget, edgar_source: SelectedDataSource) -> CompanyFinancialMetrics:
        cached = self.metrics_cache.get_valid(target.cik, SEC_METRICS_FINGERPRINT)
        if cached:
            return cached

        metrics = self.edgar_client.fetch_company_financial_metrics(
            target,
            source_strength=edgar_source.source_strength,
            source_dimension=edgar_source.dimension_id,
        )
        self.metrics_cache.save(metrics, SEC_METRICS_FINGERPRINT, self.strategy.cache_ttl_hours("edgar"))
        return metrics

    def _with_polygon_market_cap(
        self,
        target: ResolvedTarget,
        metrics: CompanyFinancialMetrics,
        polygon_source: SelectedDataSource | None,
        warnings: list[str],
    ) -> CompanyFinancialMetrics:
        if not polygon_source or not polygon_source.enabled or not self.polygon_client:
            return metrics

        document = self._polygon_document(target, polygon_source, warnings)
        if not document:
            return metrics
        if document.metadata.get("active") is False:
            _append_limited_warning(warnings, f"polygon profile marks {target.ticker} inactive")

        market_cap = document.metadata.get("market_cap")
        if not isinstance(market_cap, (int, float)) or market_cap < 0:
            return metrics

        metric_sources = {**metrics.metric_sources, "market_cap_usd": "polygon:ticker_profile:market_cap"}
        evidence = [
            *metrics.evidence,
            Evidence(
                claim=f"Polygon profile provides market capitalization for {target.canonical_name}.",
                source_type=document.source_type.value,
                source_dimension=document.source_dimension,
                source_strength=document.source_strength,
                url=document.url,
                filing_accession=document.filing_accession,
                retrieved_at=document.retrieved_at,
                quote_or_snippet=json.dumps({"ticker": target.ticker, "market_cap": market_cap}, sort_keys=True),
                verified_fact=True,
            ),
        ]
        return metrics.model_copy(
            update={
                "market_cap_usd": float(market_cap),
                "sic": metrics.sic or _polygon_sic(document),
                "metric_sources": metric_sources,
                "evidence": evidence,
            }
        )

    def _polygon_document(
        self,
        target: ResolvedTarget,
        polygon_source: SelectedDataSource,
        warnings: list[str],
    ) -> SourceDocument | None:
        cached = self.source_cache.get_valid_documents(target.cik, source_id="polygon")
        if cached:
            return cached[0]
        try:
            document = self.polygon_client.fetch_ticker_profile(
                target,
                self.strategy.cache_ttl_hours("polygon"),
                source_strength=polygon_source.source_strength,
                source_dimension=polygon_source.dimension_id,
            )
        except Exception as error:
            _append_limited_warning(warnings, f"polygon profile failed for {target.ticker}: {type(error).__name__}: {error}")
            return None
        self.source_cache.save_document(document)
        return document

    def _with_cached_polygon_market_cap(
        self,
        metrics: CompanyFinancialMetrics,
        cached_documents: list[SourceDocument],
    ) -> CompanyFinancialMetrics:
        if not cached_documents:
            return metrics
        document = cached_documents[0]
        market_cap = document.metadata.get("market_cap")
        if not isinstance(market_cap, (int, float)) or market_cap < 0:
            return metrics

        evidence = [
            *metrics.evidence,
            Evidence(
                claim=f"Cached Polygon profile provides market capitalization for {metrics.canonical_name}.",
                source_type=document.source_type.value,
                source_dimension=document.source_dimension,
                source_strength=document.source_strength,
                url=document.url,
                filing_accession=document.filing_accession,
                retrieved_at=document.retrieved_at,
                quote_or_snippet=json.dumps({"ticker": metrics.ticker, "market_cap": market_cap}, sort_keys=True),
                verified_fact=True,
            ),
        ]
        return metrics.model_copy(
            update={
                "market_cap_usd": float(market_cap),
                "sic": metrics.sic or _polygon_sic(document),
                "metric_sources": {**metrics.metric_sources, "market_cap_usd": "cache:polygon:ticker_profile:market_cap"},
                "evidence": evidence,
            }
        )


def evaluate_capacity_rules(
    candidate_metrics: CompanyFinancialMetrics,
    target_metrics: CompanyFinancialMetrics,
    settings: Settings,
) -> list[AcquirerCapacityRuleResult]:
    """Evaluate the three v1 acquisition-capacity rules independently."""

    target_market_cap = _positive_target_metric(target_metrics.market_cap_usd)
    target_revenue = _positive_target_metric(target_metrics.revenue_usd)
    return [
        _capacity_rule(
            "market_cap_to_target_market_cap",
            candidate_metrics.market_cap_usd,
            target_market_cap * settings.acquirer_market_cap_multiple if target_market_cap is not None else None,
            ["candidate_market_cap_usd"] if candidate_metrics.market_cap_usd is None else [],
            ["target_market_cap_usd"] if target_market_cap is None else [],
        ),
        _capacity_rule(
            "cash_to_target_market_cap",
            candidate_metrics.cash_and_equivalents_usd,
            target_market_cap * settings.acquirer_cash_to_target_market_cap_multiple if target_market_cap is not None else None,
            ["candidate_cash_and_equivalents_usd"] if candidate_metrics.cash_and_equivalents_usd is None else [],
            ["target_market_cap_usd"] if target_market_cap is None else [],
        ),
        _capacity_rule(
            "revenue_to_target_revenue",
            candidate_metrics.revenue_usd,
            target_revenue * settings.acquirer_revenue_multiple if target_revenue is not None else None,
            ["candidate_revenue_usd"] if candidate_metrics.revenue_usd is None else [],
            ["target_revenue_usd"] if target_revenue is None else [],
        ),
    ]


def _capacity_rule(
    rule_id: str,
    observed_value: float | None,
    threshold: float | None,
    candidate_missing_fields: list[str],
    target_missing_fields: list[str],
) -> AcquirerCapacityRuleResult:
    missing_fields = [*candidate_missing_fields, *target_missing_fields]
    if missing_fields or threshold is None:
        return AcquirerCapacityRuleResult(
            rule_id=rule_id,
            status=AcquirerCapacityRuleStatus.unknown,
            observed_value_usd=observed_value,
            threshold_usd=threshold,
            missing_fields=missing_fields,
            warning=f"{rule_id} could not be evaluated because required metrics are missing.",
        )

    ratio = observed_value / threshold if observed_value is not None and threshold > 0 else None
    return AcquirerCapacityRuleResult(
        rule_id=rule_id,
        status=AcquirerCapacityRuleStatus.passed
        if observed_value is not None and observed_value >= threshold
        else AcquirerCapacityRuleStatus.failed,
        observed_value_usd=observed_value,
        threshold_usd=threshold,
        ratio=ratio,
    )


def _thresholds(target_metrics: CompanyFinancialMetrics, settings: Settings) -> dict[str, float | None]:
    target_market_cap = _positive_target_metric(target_metrics.market_cap_usd)
    target_revenue = _positive_target_metric(target_metrics.revenue_usd)
    return {
        "candidate_market_cap_min_usd": target_market_cap * settings.acquirer_market_cap_multiple
        if target_market_cap is not None
        else None,
        "candidate_cash_min_usd": target_market_cap * settings.acquirer_cash_to_target_market_cap_multiple
        if target_market_cap is not None
        else None,
        "candidate_revenue_min_usd": target_revenue * settings.acquirer_revenue_multiple if target_revenue is not None else None,
    }


def _target_metric_warnings(target_metrics: CompanyFinancialMetrics) -> list[str]:
    warnings: list[str] = []
    if _positive_target_metric(target_metrics.market_cap_usd) is None:
        warnings.append("target market cap unavailable: market-cap and cash capacity rules will be unknown")
    if _positive_target_metric(target_metrics.revenue_usd) is None:
        warnings.append("target revenue unavailable: revenue capacity rule will be unknown")
    return warnings


def _positive_target_metric(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def _same_entity_as_target(entity: AcquirerEntity, target: ResolvedTarget) -> bool:
    return entity.cik == target.cik or (entity.ticker or "").upper() == target.ticker.upper()


def _target_from_metrics(metrics: CompanyFinancialMetrics, matched_input: str) -> ResolvedTarget:
    return ResolvedTarget(
        canonical_name=metrics.canonical_name,
        ticker=metrics.ticker,
        cik=metrics.cik,
        exchange=metrics.exchange,
        sic=metrics.sic,
        resolution_confidence=1.0,
        matched_input=matched_input,
        source_provenance=["cache:company_financial_metrics_cache"],
    )


def _is_supported_exchange(exchange: str | None) -> bool:
    return exchange in {"NYSE", "Nasdaq", "NYSE American"}


def _has_any_capacity_metric(metrics: CompanyFinancialMetrics) -> bool:
    return any(
        value is not None
        for value in (metrics.market_cap_usd, metrics.revenue_usd, metrics.cash_and_equivalents_usd)
    )


def _risk_flags(metrics: CompanyFinancialMetrics, passed_rules: list[str]) -> list[str]:
    if passed_rules == ["cash_to_target_market_cap"] and _is_financial_or_reit_like(metrics):
        return ["cash_rule_only_for_financial_or_reit_like_company"]
    return []


def _is_financial_or_reit_like(metrics: CompanyFinancialMetrics) -> bool:
    if metrics.sic and metrics.sic.startswith("6"):
        return True
    normalized_name = metrics.canonical_name.upper()
    return any(token in normalized_name for token in ("BANK", "BANCORP", "FINANCIAL", "INSURANCE", "REIT", "REALTY"))


def _best_rule_ratio(candidate: AcquirerCapabilityCandidate) -> float:
    return max((result.ratio or 0 for result in candidate.rule_results), default=0)


def _polygon_sic(document: SourceDocument) -> str | None:
    for key in ("sic_code", "sic"):
        value = document.metadata.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _append_limited_warning(warnings: list[str], warning: str, limit: int = 20) -> None:
    if len(warnings) < limit:
        warnings.append(warning)
    elif len(warnings) == limit:
        warnings.append("additional acquirer-capability warnings suppressed")
