from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.cli import main as cli_main
from src.config import Settings
from src.domain import (
    AcquirerCapabilityUniverseResult,
    AcquirerCapacityRuleStatus,
    CompanyFinancialMetrics,
    Evidence,
    ResolvedTarget,
    SourceDocument,
    SourceStrength,
    SourceType,
)
from src.pipelines.acquirer_capability import AcquirerCapabilityUniverseBuilder, evaluate_capacity_rules
from src.pipelines.acquirer_capability import AcquirerCapabilityError
from src.pipelines.target_resolution import TargetResolver
from src.repositories.database import create_session_factory, init_db
from src.repositories.financial_metrics_cache import CompanyFinancialMetricsCache
from src.repositories.source_cache import SourceCache
from src.sources.sec import financial_metrics_from_companyfacts
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "datasource_policy_path": Path("config/datasources.yaml"),
        "edgar_identity": "buyer-universe-generator/0.1 contact@example.com",
        "polygon_api_key": None,
        "news_api_key": None,
        "acquirer_cache_only_mode": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_data_source_policy_configures_acquirer_capacity_sources(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))

    sources = strategy.select("buyer_recall_acquirer_capacity", include_disabled=True)
    edgar_source = next(source for source in sources if source.source_id == "edgar")
    polygon_source = next(source for source in sources if source.source_id == "polygon")

    assert edgar_source.enabled is True
    assert edgar_source.source_strength == SourceStrength.A
    assert {"public_company_universe", "revenue", "cash", "xbrl_companyfacts"} <= set(edgar_source.field_coverage)
    assert polygon_source.enabled is False
    assert polygon_source.disabled_reason == "missing BUG_POLYGON_API_KEY"
    assert "market_cap" in polygon_source.field_coverage


def test_companyfacts_extracts_latest_annual_revenue_and_cash() -> None:
    target = sample_target()
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            revenue_fact(100_000_000, "2024-12-31", filed="2025-02-01", fy=2024),
                            revenue_fact(150_000_000, "2025-12-31", filed="2026-02-01", fy=2025),
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            cash_fact(20_000_000, "2025-09-30", filed="2025-10-15", fy=2025, fp="Q3"),
                            cash_fact(30_000_000, "2025-12-31", filed="2026-02-01", fy=2025, fp="FY"),
                        ]
                    }
                },
            }
        }
    }

    metrics = financial_metrics_from_companyfacts(
        target,
        payload,
        source_strength=SourceStrength.A,
        source_dimension="buyer_long_list_recall.acquirer_capacity",
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
    )

    assert metrics.revenue_usd == 150_000_000
    assert metrics.revenue_period_type == "FY"
    assert metrics.cash_and_equivalents_usd == 30_000_000
    assert metrics.metric_sources["revenue_usd"].startswith("sec_companyfacts")
    assert {evidence.source_type for evidence in metrics.evidence} == {SourceType.sec_companyfacts.value}


def test_companyfacts_falls_back_to_ttm_discrete_quarters() -> None:
    target = sample_target()
    payload = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            revenue_fact(10_000_000, "2025-03-31", start="2025-01-01", fy=2025, fp="Q1", frame="CY2025Q1"),
                            revenue_fact(12_000_000, "2025-06-30", start="2025-04-01", fy=2025, fp="Q2", frame="CY2025Q2"),
                            revenue_fact(14_000_000, "2025-09-30", start="2025-07-01", fy=2025, fp="Q3", frame="CY2025Q3"),
                            revenue_fact(16_000_000, "2025-12-31", start="2025-10-01", fy=2025, fp="Q4", frame="CY2025Q4"),
                        ]
                    }
                }
            }
        }
    }

    metrics = financial_metrics_from_companyfacts(
        target,
        payload,
        source_strength=SourceStrength.A,
        source_dimension="buyer_long_list_recall.acquirer_capacity",
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
    )

    assert metrics.revenue_usd == 52_000_000
    assert metrics.revenue_period_type == "TTM"
    assert "TTM components" in metrics.evidence[0].quote_or_snippet


def test_capacity_rules_keep_missing_metrics_unknown(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    target_metrics = sample_metrics(sample_target(), revenue=None, cash=None, market_cap=None)
    candidate_metrics = sample_metrics(company("BIG", "0000000002", "Big Buyer Inc."), revenue=500, cash=None, market_cap=None)

    results = evaluate_capacity_rules(candidate_metrics, target_metrics, settings)

    assert [result.status for result in results] == [
        AcquirerCapacityRuleStatus.unknown,
        AcquirerCapacityRuleStatus.unknown,
        AcquirerCapacityRuleStatus.unknown,
    ]
    assert results[0].missing_fields == ["candidate_market_cap_usd", "target_market_cap_usd"]
    assert results[2].missing_fields == ["target_revenue_usd"]


def test_acquirer_capability_builder_filters_and_returns_passing_candidates(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, polygon_api_key="polygon-test-key")
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_sec = FakeAcquirerSecClient()
    fake_polygon = FakeAcquirerPolygonClient({"TGT": 1_000, "BIG": 1_000, "CASHB": 100, "SMALL": 100})

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        source_cache = SourceCache(session)
        builder = AcquirerCapabilityUniverseBuilder(
            settings=settings,
            strategy=strategy,
            resolver=TargetResolver(strategy=strategy, edgar_client=fake_sec, source_cache=source_cache),
            source_cache=source_cache,
            metrics_cache=CompanyFinancialMetricsCache(session),
            edgar_client=fake_sec,
            polygon_client=fake_polygon,
        )
        result = builder.build_universe("TGT")

    assert [candidate.ticker for candidate in result.candidates] == ["BIG", "CASHB"]
    assert result.candidates[0].passed_rules == ["revenue_to_target_revenue"]
    assert result.candidates[1].passed_rules == ["cash_to_target_market_cap"]
    assert result.candidates[1].risk_flags == ["cash_rule_only_for_financial_or_reit_like_company"]
    assert result.excluded_counts["target_self"] == 1
    assert result.excluded_counts["unsupported_exchange"] == 1
    assert result.excluded_counts["non_operating_security"] == 1
    assert result.excluded_counts["capacity_failed"] == 1
    assert result.excluded_counts["metrics_unavailable"] == 1
    assert result.thresholds == {
        "candidate_market_cap_min_usd": 3_000.0,
        "candidate_cash_min_usd": 300.0,
        "candidate_revenue_min_usd": 200.0,
    }
    assert result.source_coverage["polygon_market_cap_available"] == 3


def test_acquirer_capability_cache_only_mode_uses_cached_metrics_without_external_reads(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, acquirer_cache_only_mode=True)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    target = sample_target()
    buyer = company("BIG", "0000000002", "Big Buyer Inc.")

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        source_cache = SourceCache(session)
        metrics_cache = CompanyFinancialMetricsCache(session)
        metrics_cache.save(sample_metrics(target, revenue=100, cash=20, market_cap=None), "sec-companyfacts-financial-metrics-v1", 24)
        metrics_cache.save(sample_metrics(buyer, revenue=300, cash=10, market_cap=None), "sec-companyfacts-financial-metrics-v1", 24)
        source_cache.save_document(
            SourceDocument(
                source_id="polygon",
                source_dimension="buyer_long_list_recall.acquirer_capacity_screening",
                source_type=SourceType.exchange_profile,
                source_strength=SourceStrength.B,
                target_cik=target.cik,
                target_ticker=target.ticker,
                url="https://api.polygon.io/v3/reference/tickers/TGT",
                metadata={"ticker": "TGT", "market_cap": 1_000},
                retrieved_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        builder = AcquirerCapabilityUniverseBuilder(
            settings=settings,
            strategy=strategy,
            resolver=TargetResolver(strategy=strategy, edgar_client=ExplodingSecClient(), source_cache=source_cache),
            source_cache=source_cache,
            metrics_cache=metrics_cache,
            edgar_client=ExplodingSecClient(),
            polygon_client=ExplodingPolygonClient(),
        )
        result = builder.build_universe("TGT")

    assert result.target.source_provenance == ["cache:company_financial_metrics_cache"]
    assert result.source_coverage["cache_only_mode"] == 1
    assert result.source_coverage["cached_metric_companies"] == 2
    assert result.warnings[0] == "acquirer cache-only mode enabled: no new SEC or Polygon data will be requested"
    assert [candidate.ticker for candidate in result.candidates] == ["BIG"]
    assert result.candidates[0].passed_rules == ["revenue_to_target_revenue"]


def test_acquirer_capability_cache_only_mode_batch_reads_polygon_cache(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, acquirer_cache_only_mode=True)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    target = sample_target()
    buyer = company("BIG", "0000000002", "Big Buyer Inc.")

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        source_cache = CountingSourceCache(session)
        metrics_cache = CompanyFinancialMetricsCache(session)
        metrics_cache.save(sample_metrics(target, revenue=100, cash=20, market_cap=None), "sec-companyfacts-financial-metrics-v1", 24)
        metrics_cache.save(sample_metrics(buyer, revenue=300, cash=10, market_cap=None), "sec-companyfacts-financial-metrics-v1", 24)
        source_cache.save_documents(
            [
                polygon_document(target, 1_000),
                polygon_document(buyer, 1_000),
            ]
        )
        builder = AcquirerCapabilityUniverseBuilder(
            settings=settings,
            strategy=strategy,
            resolver=TargetResolver(strategy=strategy, edgar_client=ExplodingSecClient(), source_cache=source_cache),
            source_cache=source_cache,
            metrics_cache=metrics_cache,
            edgar_client=ExplodingSecClient(),
            polygon_client=ExplodingPolygonClient(),
        )
        result = builder.build_universe("TGT")

    assert [candidate.ticker for candidate in result.candidates] == ["BIG"]
    assert source_cache.batch_document_calls == 1
    assert source_cache.single_document_calls == 0


def test_acquirer_capability_cache_only_mode_requires_cached_target(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, acquirer_cache_only_mode=True)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        source_cache = SourceCache(session)
        builder = AcquirerCapabilityUniverseBuilder(
            settings=settings,
            strategy=strategy,
            resolver=TargetResolver(strategy=strategy, edgar_client=ExplodingSecClient(), source_cache=source_cache),
            source_cache=source_cache,
            metrics_cache=CompanyFinancialMetricsCache(session),
            edgar_client=ExplodingSecClient(),
            polygon_client=ExplodingPolygonClient(),
        )
        try:
            builder.build_universe("MISSING")
        except AcquirerCapabilityError as error:
            assert error.error_code == "acquirer_cache_only_target_not_cached"
        else:
            raise AssertionError("cache-only mode should require the target in the metrics cache")


def test_acquirer_capability_api_returns_universe(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for_tests(tmp_path)
    monkeypatch.setattr("src.api.main.build_acquirer_capability_universe_builder", lambda _settings, _session: FakeAcquirerService())
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/buyers/acquirer-capable-universe?query=TGT")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target"]["ticker"] == "TGT"
    assert payload["candidates"][0]["ticker"] == "BIG"


def test_build_acquirer_capable_universe_cli_outputs_json(tmp_path: Path, monkeypatch, capsys) -> None:
    settings = settings_for_tests(tmp_path)
    monkeypatch.setattr("src.cli.get_settings", lambda: settings)
    monkeypatch.setattr("src.cli.build_acquirer_capability_universe_builder", lambda _settings, _session: FakeAcquirerService())

    exit_code = cli_main(["build-acquirer-capable-universe", "TGT"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["target"]["ticker"] == "TGT"
    assert payload["candidates"][0]["ticker"] == "BIG"


def sample_target() -> ResolvedTarget:
    return company("TGT", "0000000001", "Target Inc.")


def company(ticker: str, cik: str, name: str, exchange: str = "NYSE", sic: str | None = None) -> ResolvedTarget:
    return ResolvedTarget(
        canonical_name=name,
        ticker=ticker,
        cik=cik,
        exchange=exchange,
        sic=sic,
        resolution_confidence=1.0,
        matched_input=ticker,
        source_provenance=["edgar:company_tickers_exchange"],
    )


def sample_metrics(
    target: ResolvedTarget,
    *,
    revenue: float | None,
    cash: float | None,
    market_cap: float | None,
    sic: str | None = None,
) -> CompanyFinancialMetrics:
    evidence = []
    if any(value is not None for value in (revenue, cash, market_cap)):
        evidence.append(
            Evidence(
                claim=f"{target.canonical_name} has source-backed capacity metrics.",
                source_type=SourceType.sec_companyfacts.value,
                source_strength=SourceStrength.A,
                url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{target.cik}.json",
                quote_or_snippet="test metrics",
            )
        )
    return CompanyFinancialMetrics(
        canonical_name=target.canonical_name,
        ticker=target.ticker,
        cik=target.cik,
        exchange=target.exchange,
        sic=sic or target.sic,
        market_cap_usd=market_cap,
        revenue_usd=revenue,
        cash_and_equivalents_usd=cash,
        revenue_period_end="2025-12-31" if revenue is not None else None,
        cash_period_end="2025-12-31" if cash is not None else None,
        evidence=evidence,
    )


def polygon_document(target: ResolvedTarget, market_cap: float) -> SourceDocument:
    return SourceDocument(
        source_id="polygon",
        source_dimension="buyer_long_list_recall.acquirer_capacity_screening",
        source_type=SourceType.exchange_profile,
        source_strength=SourceStrength.B,
        target_cik=target.cik,
        target_ticker=target.ticker,
        url=f"https://api.polygon.io/v3/reference/tickers/{target.ticker}",
        metadata={"ticker": target.ticker, "market_cap": market_cap},
        retrieved_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )


def revenue_fact(
    value: float,
    end: str,
    *,
    start: str = "2025-01-01",
    filed: str = "2026-02-01",
    fy: int = 2025,
    fp: str = "FY",
    frame: str | None = None,
) -> dict[str, Any]:
    fact = {
        "val": value,
        "start": start,
        "end": end,
        "filed": filed,
        "form": "10-K" if fp == "FY" else "10-Q",
        "fy": fy,
        "fp": fp,
    }
    if frame:
        fact["frame"] = frame
    return fact


def cash_fact(value: float, end: str, *, filed: str, fy: int, fp: str) -> dict[str, Any]:
    return {"val": value, "end": end, "filed": filed, "form": "10-K" if fp == "FY" else "10-Q", "fy": fy, "fp": fp}


class FakeAcquirerSecClient:
    def __init__(self) -> None:
        self.target = sample_target()
        self.targets = [
            self.target,
            company("BIG", "0000000002", "Big Buyer Inc."),
            company("CASHB", "0000000003", "Cash Bank Inc.", sic="6021"),
            company("SMALL", "0000000004", "Small Buyer Inc."),
            company("NOINFO", "0000000005", "No Info Inc."),
            company("OTCB", "0000000006", "OTC Buyer Inc.", exchange="OTC"),
            company("ETF", "0000000007", "SPDR Test ETF Trust"),
        ]
        self.metrics = {
            "TGT": sample_metrics(self.target, revenue=100, cash=50, market_cap=None),
            "BIG": sample_metrics(self.targets[1], revenue=300, cash=10, market_cap=None),
            "CASHB": sample_metrics(self.targets[2], revenue=50, cash=400, market_cap=None, sic="6021"),
            "SMALL": sample_metrics(self.targets[3], revenue=100, cash=50, market_cap=None),
            "NOINFO": sample_metrics(self.targets[4], revenue=None, cash=None, market_cap=None),
        }

    def resolve_by_ticker(self, ticker: str) -> ResolvedTarget | None:
        return self.target if ticker.upper() == "TGT" else None

    def resolve_by_cik(self, cik: str) -> ResolvedTarget | None:
        return self.target if cik == self.target.cik else None

    def resolve_by_company_name(self, company_name: str) -> list[ResolvedTarget]:
        return [self.target] if company_name == self.target.canonical_name else []

    def fetch_public_company_universe(self) -> list[ResolvedTarget]:
        return self.targets

    def fetch_company_financial_metrics(self, target: ResolvedTarget, **_kwargs) -> CompanyFinancialMetrics:
        return self.metrics.get(target.ticker, sample_metrics(target, revenue=None, cash=None, market_cap=None))


class FakeAcquirerPolygonClient:
    def __init__(self, market_caps: dict[str, float]) -> None:
        self.market_caps = market_caps

    def fetch_ticker_profile(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        return SourceDocument(
            source_id="polygon",
            source_dimension=source_dimension,
            source_type=SourceType.exchange_profile,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=f"https://api.polygon.io/v3/reference/tickers/{target.ticker}",
            metadata={"ticker": target.ticker, "market_cap": self.market_caps.get(target.ticker), "active": True},
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )


class CountingSourceCache(SourceCache):
    def __init__(self, session) -> None:
        super().__init__(session)
        self.single_document_calls = 0
        self.batch_document_calls = 0

    def get_valid_documents(self, target_cik: str, source_id: str | None = None) -> list[SourceDocument]:
        self.single_document_calls += 1
        return super().get_valid_documents(target_cik, source_id)

    def get_valid_documents_by_cik(
        self,
        target_ciks: list[str],
        source_id: str | None = None,
    ) -> dict[str, list[SourceDocument]]:
        self.batch_document_calls += 1
        return super().get_valid_documents_by_cik(target_ciks, source_id)


class ExplodingSecClient:
    def resolve_by_ticker(self, *_args, **_kwargs):
        raise AssertionError("cache-only mode must not resolve through SEC")

    def resolve_by_cik(self, *_args, **_kwargs):
        raise AssertionError("cache-only mode must not resolve through SEC")

    def resolve_by_company_name(self, *_args, **_kwargs):
        raise AssertionError("cache-only mode must not resolve through SEC")

    def fetch_public_company_universe(self):
        raise AssertionError("cache-only mode must not fetch the SEC public-company universe")

    def fetch_company_financial_metrics(self, *_args, **_kwargs):
        raise AssertionError("cache-only mode must not fetch SEC companyfacts")


class ExplodingPolygonClient:
    def fetch_ticker_profile(self, *_args, **_kwargs):
        raise AssertionError("cache-only mode must not fetch Polygon ticker profiles")


class FakeAcquirerService:
    def build_universe(self, _query: str) -> AcquirerCapabilityUniverseResult:
        target = sample_target()
        target_metrics = sample_metrics(target, revenue=100, cash=50, market_cap=1_000)
        buyer = company("BIG", "0000000002", "Big Buyer Inc.")
        buyer_metrics = sample_metrics(buyer, revenue=300, cash=10, market_cap=1_000)
        rule_results = evaluate_capacity_rules(buyer_metrics, target_metrics, Settings())
        return AcquirerCapabilityUniverseResult(
            target=target,
            target_metrics=target_metrics,
            thresholds={
                "candidate_market_cap_min_usd": 3_000,
                "candidate_cash_min_usd": 300,
                "candidate_revenue_min_usd": 200,
            },
            candidates=[
                {
                    "canonical_name": buyer.canonical_name,
                    "ticker": buyer.ticker,
                    "cik": buyer.cik,
                    "exchange": buyer.exchange,
                    "sic": buyer.sic,
                    "metrics": buyer_metrics,
                    "rule_results": rule_results,
                    "passed_rules": ["revenue_to_target_revenue"],
                    "evidence": buyer_metrics.evidence,
                }
            ],
        )
