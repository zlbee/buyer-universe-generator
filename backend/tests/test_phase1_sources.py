from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.config import Settings
from src.domain import FilingMetadata, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.pipelines.source_ingestion import SourceIngestionService
from src.pipelines.target_resolution import AmbiguousTargetError, TargetNotFoundError, TargetResolver
from src.repositories.database import create_session_factory, init_db
from src.repositories.source_cache import SourceCache
from src.sources.newsapi import NewsApiClient
from src.sources.polygon import PolygonClient
from src.sources.sec import SecEdgarClient
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
    }
    values.update(overrides)
    return Settings(**values)


def test_data_source_policy_disables_optional_sources_without_keys(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))

    target_sources = strategy.select("target_resolution", include_disabled=True)
    assert target_sources[0].source_id == "edgar"
    assert target_sources[0].dimension_id == "seller_profile.identity_resolution"
    assert target_sources[0].source_strength == SourceStrength.A
    assert target_sources[0].enabled is True

    polygon_source = next(source for source in target_sources if source.source_id == "polygon")
    assert polygon_source.dimension_id == "seller_profile.exchange_profile"
    assert polygon_source.source_strength == SourceStrength.B
    assert polygon_source.enabled is False
    assert polygon_source.disabled_reason == "missing BUG_POLYGON_API_KEY"

    news_source = strategy.select("news_discovery", include_disabled=True)[0]
    assert news_source.dimension_id == "seller_profile.recent_news_context"
    assert news_source.source_strength == SourceStrength.B
    assert news_source.enabled is False
    assert news_source.disabled_reason == "missing BUG_NEWS_API_KEY"
    assert {"bloomberg.com", "reuters.com", "wsj.com"} <= set(news_source.config.retrieval.domains)


def test_data_source_policy_scopes_strength_by_dimension(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))

    identity_source = strategy.selected_source("target_resolution", "edgar", include_disabled=True)
    transaction_source = strategy.selected_source("buyer_recall_transaction_signals", "edgar", include_disabled=True)
    news_context_source = strategy.selected_source("news_discovery", "newsapi", include_disabled=True)
    sponsor_source = strategy.selected_source("buyer_recall_financial_sponsors", "newsapi", include_disabled=True)

    assert identity_source is not None
    assert transaction_source is not None
    assert news_context_source is not None
    assert sponsor_source is not None
    assert identity_source.source_strength == SourceStrength.A
    assert transaction_source.source_strength == SourceStrength.C
    assert news_context_source.source_strength == SourceStrength.B
    assert sponsor_source.source_strength == SourceStrength.C


def test_sec_client_resolves_mapping_and_recent_filings(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("company_tickers_exchange.json"):
            return httpx.Response(
                200,
                json={
                    "fields": ["cik", "name", "ticker", "exchange"],
                    "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
                },
            )
        if str(request.url).endswith("CIK0000320193.json"):
            return httpx.Response(
                200,
                json={
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-Q", "8-K", "8-K"],
                            "accessionNumber": [
                                "0000320193-25-000001",
                                "0000320193-25-000002",
                                "0000320193-25-000003",
                                "0000320193-25-000004",
                            ],
                            "filingDate": ["2025-10-31", "2025-07-31", "2025-06-01", "2025-05-01"],
                            "reportDate": ["2025-09-30", "2025-06-30", "2025-05-31", "2025-04-30"],
                        }
                    }
                },
            )
        return httpx.Response(404)

    client = SecEdgarClient(
        settings_for_tests(tmp_path),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )

    target = client.resolve_by_ticker("aapl")
    assert target is not None
    assert target.cik == "0000320193"
    assert target.exchange == "Nasdaq"

    same_target = client.resolve_by_company_name("Apple Inc.")[0]
    assert same_target.cik == target.cik

    filings = client.fetch_recent_filings(target)
    assert [filing.form for filing in filings] == ["10-K", "10-Q", "8-K"]
    assert filings[0].url and "Archives/edgar/data/320193" in filings[0].url


def test_sec_client_uses_edgartools_for_resolution_and_filings(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarModule()
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    target = client.resolve_by_ticker("AAPL")
    assert target is not None
    assert target.cik == "0000320193"
    assert target.sic == "3571"
    assert target.source_provenance == ["edgartools:Company"]
    assert fake_edgar.identity == "buyer-universe-generator/0.1 contact@example.com"

    company_matches = client.resolve_by_company_name("Apple Inc.")
    assert len(company_matches) == 1
    assert company_matches[0].source_provenance == ["edgartools:find_company"]

    filings = client.fetch_recent_filings(target)
    assert [filing.form for filing in filings] == ["10-K", "10-Q", "8-K"]
    assert filings[0].accession_number == "0000320193-25-000001"
    assert fake_edgar.company_calls == ["AAPL", "0000320193"]


def test_polygon_client_returns_supporting_profile_document(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apiKey"] == "polygon-test-key"
        return httpx.Response(200, json={"results": {"ticker": "AAPL", "market_cap": 1_000_000}})

    settings = settings_for_tests(tmp_path, polygon_api_key="polygon-test-key")
    client = PolygonClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    document = client.fetch_ticker_profile(sample_target(), ttl_hours=24)

    assert document.source_id == "polygon"
    assert document.source_type == SourceType.exchange_profile
    assert document.source_strength == SourceStrength.C
    assert document.metadata["market_cap"] == 1_000_000


def test_newsapi_client_returns_article_documents(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    domains = ["bloomberg.com", "reuters.com", "wsj.com"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apiKey"] == "news-test-key"
        assert request.url.params["domains"] == ",".join(domains)
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "title": "Apple acquisition article",
                        "url": "https://example.com/apple-news",
                        "publishedAt": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        )

    settings = settings_for_tests(tmp_path, news_api_key="news-test-key")
    client = NewsApiClient(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level(logging.INFO):
        documents = client.fetch_target_articles(sample_target(), ttl_hours=12, domains=domains)

    assert len(documents) == 1
    assert documents[0].source_id == "newsapi"
    assert documents[0].source_type == SourceType.news_article
    assert documents[0].source_strength == SourceStrength.B
    newsapi_log_text = "\n".join(
        record.message for record in caplog.records if record.name in {"src.sources.newsapi", "uvicorn.error"}
    )
    assert "NewsAPI request" in newsapi_log_text
    assert ",".join(domains) in newsapi_log_text
    assert "news-test-key" not in newsapi_log_text
    assert "<redacted>" in newsapi_log_text
    assert any(record.name == "uvicorn.error" and "NewsAPI request" in record.message for record in caplog.records)


def test_source_ingestion_applies_newsapi_domains_from_policy(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key="news-test-key")
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_sec = FakeSecClient()
    fake_news = FakeNewsApiClient()

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        cache = SourceCache(session)
        resolver = TargetResolver(strategy=strategy, edgar_client=fake_sec, source_cache=cache)
        service = SourceIngestionService(
            strategy=strategy,
            resolver=resolver,
            cache=cache,
            edgar_client=fake_sec,
            newsapi_client=fake_news,
        )

        result = service.ingest("AAPL")

    assert fake_news.domains is not None
    assert {"bloomberg.com", "reuters.com", "wsj.com"} <= set(fake_news.domains)
    assert any(document.source_id == "newsapi" for document in result.source_documents)


def test_target_resolver_handles_ticker_exact_name_invalid_and_ambiguous(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))
    resolver = TargetResolver(strategy=strategy, edgar_client=FakeSecClient())

    ticker_target = resolver.resolve("AAPL")
    name_target = resolver.resolve("Apple Inc.")
    assert ticker_target.cik == name_target.cik

    with pytest.raises(TargetNotFoundError):
        resolver.resolve("missing target")

    with pytest.raises(AmbiguousTargetError) as ambiguous:
        resolver.resolve("Duplicate Inc.")
    assert len(ambiguous.value.candidates) == 2


def test_source_ingestion_persists_and_reuses_cached_documents(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_sec = FakeSecClient()

    with session_factory() as session:
        strategy = DataSourceStrategy.from_settings(settings)
        cache = SourceCache(session)
        resolver = TargetResolver(strategy=strategy, edgar_client=fake_sec, source_cache=cache)
        service = SourceIngestionService(
            strategy=strategy,
            resolver=resolver,
            cache=cache,
            edgar_client=fake_sec,
        )

        first_result = service.ingest("AAPL")
        second_result = service.ingest("AAPL")

    assert first_result.target.ticker == "AAPL"
    assert [filing.form for filing in first_result.filings] == ["10-K", "10-Q", "8-K"]
    assert len(first_result.source_documents) == 4
    assert len(second_result.source_documents) == 4
    assert fake_sec.filing_calls == 1
    assert "polygon disabled: missing BUG_POLYGON_API_KEY" in first_result.warnings
    assert "newsapi disabled: missing BUG_NEWS_API_KEY" in first_result.warnings


def sample_target() -> ResolvedTarget:
    return ResolvedTarget(
        canonical_name="Apple Inc.",
        ticker="AAPL",
        cik="0000320193",
        exchange="Nasdaq",
        resolution_confidence=1.0,
        matched_input="AAPL",
        source_provenance=["edgar:company_tickers_exchange"],
    )


class FakeSecClient:
    def __init__(self) -> None:
        self.filing_calls = 0
        self.target = sample_target()

    def resolve_by_ticker(self, ticker: str) -> ResolvedTarget | None:
        return self.target if ticker.upper() == "AAPL" else None

    def resolve_by_cik(self, cik: str) -> ResolvedTarget | None:
        return self.target if cik in {"320193", "0000320193"} else None

    def resolve_by_company_name(self, company_name: str) -> list[ResolvedTarget]:
        if company_name == "Apple Inc.":
            return [self.target]
        if company_name == "Duplicate Inc.":
            return [
                self.target.model_copy(update={"ticker": "DUPA", "cik": "0000000001"}),
                self.target.model_copy(update={"ticker": "DUPB", "cik": "0000000002"}),
            ]
        return []

    def fetch_recent_filings(self, target: ResolvedTarget) -> list[FilingMetadata]:
        self.filing_calls += 1
        return [
            FilingMetadata(form="10-K", filing_date="2026-01-01", accession_number="0000320193-26-000001"),
            FilingMetadata(form="10-Q", filing_date="2026-02-01", accession_number="0000320193-26-000002"),
            FilingMetadata(form="8-K", filing_date="2026-03-01", accession_number="0000320193-26-000003"),
        ]

    def source_document_for_mapping(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        return SourceDocument(
            source_id="edgar",
            source_dimension=source_dimension,
            source_type=SourceType.sec_company_mapping,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            metadata=target.model_dump(mode="json"),
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )

    def source_document_for_filing(
        self,
        target: ResolvedTarget,
        filing: FilingMetadata,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        return SourceDocument(
            source_id="edgar",
            source_dimension=source_dimension,
            source_type=SourceType.sec_filing,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            filing_accession=filing.accession_number,
            metadata=filing.model_dump(mode="json"),
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )


class FakeNewsApiClient:
    def __init__(self) -> None:
        self.domains: list[str] | None = None

    def fetch_target_articles(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 5,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
        domains: list[str] | None = None,
    ) -> list[SourceDocument]:
        self.domains = domains
        return [
            SourceDocument(
                source_id="newsapi",
                source_dimension=source_dimension,
                source_type=SourceType.news_article,
                source_strength=source_strength,
                target_cik=target.cik,
                target_ticker=target.ticker,
                url="https://reuters.com/apple-news",
                metadata={"domains": domains, "page_size": page_size},
                retrieved_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            )
        ]


class FakeEdgarModule:
    def __init__(self) -> None:
        self.identity: str | None = None
        self.company_calls: list[str] = []

    def set_identity(self, identity: str) -> None:
        self.identity = identity

    def Company(self, identifier: str):
        self.company_calls.append(identifier)
        if identifier in {"AAPL", "0000320193"}:
            return FakeEdgarCompany()
        raise ValueError(identifier)

    def find_company(self, company_name: str, top_n: int = 10):
        assert top_n == 10
        if company_name == "Apple Inc.":
            return FakeEdgarSearchResults(
                [
                    {"cik": 320193, "ticker": "AAPL", "company": "Apple Inc.", "score": 99},
                ]
            )
        return FakeEdgarSearchResults([])


class FakeEdgarSearchResults:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.results = rows


class FakeEdgarCompany:
    cik = 320193
    name = "Apple Inc."
    tickers = ["AAPL"]
    exchanges = ["Nasdaq"]
    sic = 3571

    def get_filings(self, form: str, trigger_full_load: bool = False):
        assert trigger_full_load is False
        return FakeEdgarFilings(form)


class FakeEdgarFilings:
    def __init__(self, form: str) -> None:
        self.form = form

    def latest(self):
        accession_by_form = {
            "10-K": "0000320193-25-000001",
            "10-Q": "0000320193-25-000002",
            "8-K": "0000320193-25-000003",
        }
        return FakeEdgarFiling(self.form, accession_by_form[self.form])


class FakeEdgarFiling:
    def __init__(self, form: str, accession_no: str) -> None:
        self.form = form
        self.accession_no = accession_no
        self.filing_date = "2025-10-31"
        self.report_date = "2025-09-30"
