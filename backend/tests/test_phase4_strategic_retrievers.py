from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.config import Settings
from src.domain import (
    BuyerType,
    CandidateHit,
    Evidence,
    SourceDocument,
    SourceStrength,
    SourceType,
    StrategicRetrievalResult,
    TargetProfile,
)
from src.pipelines.orchestrator import PipelineOrchestrator
from src.retrievers import BuyerCandidateRetriever, MAHistoryRetriever, SameSicRetriever
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "datasource_policy_path": Path("config/datasources.yaml"),
        "keyword_taxonomy_path": Path("config/keyword_taxonomy.yaml"),
        "edgar_identity": "buyer-universe-generator/0.1 contact@example.com",
        "polygon_api_key": None,
        "news_api_key": None,
        "openrouter_api_key": "openrouter-test-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_same_sic_retriever_recalls_only_traceable_non_self_same_sic(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    retriever = SameSicRetriever(strategy, FakeSicSource(same_sic_documents()))

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Beauty Buyer Inc."]
    hit = result.hits[0]
    assert hit.buyer_type == BuyerType.strategic
    assert hit.candidate_ticker == "BBY"
    assert hit.candidate_cik == "0000002000"
    assert hit.source_path == ["same_sic"]
    assert hit.retrieval_metadata["target_sic"] == "2844"
    assert hit.retrieval_metadata["candidate_sic"] == "2844"
    assert hit.evidence[0].source_dimension == "buyer_long_list_recall.public_company_peer_discovery"
    assert any("polygon disabled" in warning for warning in result.warnings)
    assert any("without traceable evidence" in warning for warning in result.warnings)


def test_sec_client_can_fallback_to_browse_edgar_by_sic(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://www.sec.gov/files/company_tickers_exchange.json"):
            return httpx.Response(200, json={"fields": ["cik", "name", "ticker", "exchange"], "data": []})
        return httpx.Response(
            200,
            text=(
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry>"
                "<title>Beauty Buyer Inc. CIK: 2000</title>"
                "<summary>SIC 2844 public company result</summary>"
                "<link href='https://www.sec.gov/cgi-bin/browse-edgar?CIK=2000' />"
                "</entry>"
                "</feed>"
            ),
        )

    client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )

    documents = client.list_public_company_profiles_by_sic(
        "2844",
        limit=5,
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="buyer_long_list_recall.public_company_peer_discovery",
    )

    assert len(documents) == 1
    assert documents[0].metadata["canonical_name"] == "Beauty Buyer Inc."
    assert documents[0].metadata["cik"] == "0000002000"
    assert documents[0].metadata["sic"] == "2844"
    assert documents[0].url == "https://www.sec.gov/cgi-bin/browse-edgar?CIK=2000"


def test_sec_client_enriches_browse_sic_records_from_company_mapping(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://www.sec.gov/files/company_tickers_exchange.json"):
            return httpx.Response(
                200,
                json={
                    "fields": ["cik", "name", "ticker", "exchange"],
                    "data": [[724989, "Beauty Mapping Co.", "BMAP", "NYSE"]],
                },
            )
        return httpx.Response(
            200,
            text=(
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry title='ARRAY(0x123)'>"
                "<content type='text/xml'>"
                "<company-info name='ARRAY(0x456)'><cik>0000724989</cik><sic>2844</sic><state>NJ</state></company-info>"
                "</content>"
                "<link href='https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000724989' />"
                "<summary type='html'>&lt;strong&gt;CIK:&lt;/strong&gt; 0000724989, &lt;strong&gt;State:&lt;/strong&gt; NJ</summary>"
                "</entry>"
                "</feed>"
            ),
        )

    client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )

    documents = client.list_public_company_profiles_by_sic(
        "2844",
        limit=5,
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="buyer_long_list_recall.public_company_peer_discovery",
    )

    assert len(documents) == 1
    assert documents[0].metadata["canonical_name"] == "Beauty Mapping Co."
    assert documents[0].metadata["ticker"] == "BMAP"
    assert documents[0].metadata["exchange"] == "NYSE"
    assert documents[0].metadata["sic"] == "2844"
    assert documents[0].metadata["provider_method"] == "sec_browse_edgar_by_sic_enriched_with_company_mapping"


def test_sec_client_enriches_browse_sic_records_from_submissions_when_mapping_has_no_name(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers_exchange.json"):
            return httpx.Response(200, json={"fields": ["cik", "name", "ticker", "exchange"], "data": []})
        if url.startswith("https://data.sec.gov/submissions/CIK0000724989.json"):
            return httpx.Response(
                200,
                json={
                    "cik": "0000724989",
                    "name": "ADRien Arpel Inc",
                    "tickers": ["AAPEL"],
                    "exchanges": ["OTC"],
                    "sic": "2844",
                    "sicDescription": "Perfumes, Cosmetics & Other Toilet Preparations",
                    "filings": {"recent": {}},
                },
            )
        return httpx.Response(
            200,
            text=(
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry title='ARRAY(0x123)'>"
                "<content type='text/xml'>"
                "<company-info name='ARRAY(0x456)'><cik>0000724989</cik><sic>2844</sic><state>NJ</state></company-info>"
                "</content>"
                "<link href='https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000724989' />"
                "<summary type='html'>&lt;strong&gt;CIK:&lt;/strong&gt; 0000724989, &lt;strong&gt;State:&lt;/strong&gt; NJ</summary>"
                "</entry>"
                "</feed>"
            ),
        )

    client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )

    documents = client.list_public_company_profiles_by_sic(
        "2844",
        limit=5,
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="buyer_long_list_recall.public_company_peer_discovery",
    )

    assert len(documents) == 1
    assert documents[0].metadata["canonical_name"] == "ADRien Arpel Inc"
    assert documents[0].metadata["ticker"] == "AAPEL"
    assert documents[0].metadata["exchange"] == "OTC"
    assert documents[0].metadata["sic"] == "2844"
    assert documents[0].metadata["provider_method"] == "sec_browse_edgar_by_sic_enriched_with_submissions"


def test_sec_client_fetches_same_sic_8k_transaction_signals(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        url = str(request.url)
        if url.startswith("https://www.sec.gov/files/company_tickers_exchange.json"):
            return httpx.Response(
                200,
                json={
                    "fields": ["cik", "name", "ticker", "exchange", "sic"],
                    "data": [
                        [2000, "BeautyCo", "BTY", "NYSE", "2844"],
                        [1600033, "e.l.f. Beauty, Inc.", "ELF", "NYSE", "2844"],
                    ],
                },
            )
        if url.startswith("https://data.sec.gov/submissions/CIK0000002000.json"):
            return httpx.Response(
                200,
                json={
                    "cik": "0000002000",
                    "name": "BeautyCo",
                    "tickers": ["BTY"],
                    "exchanges": ["NYSE"],
                    "sic": "2844",
                    "filings": {
                        "recent": {
                            "form": ["8-K", "8-K", "8-K"],
                            "filingDate": ["2025-06-01", "2025-07-01", "2020-01-01"],
                            "reportDate": ["2025-06-01", "2025-07-01", "2020-01-01"],
                            "accessionNumber": [
                                "0000002000-25-000001",
                                "0000002000-25-000002",
                                "0000002000-20-000001",
                            ],
                            "items": ["2.01,9.01", "1.01,9.01", "2.01,9.01"],
                            "primaryDocument": ["bty-20250601.htm", "bty-20250701.htm", "bty-20200101.htm"],
                        }
                    },
                },
            )
        if url.startswith("https://data.sec.gov/submissions/CIK0001600033.json"):
            return httpx.Response(
                200,
                json={
                    "cik": "0001600033",
                    "name": "e.l.f. Beauty, Inc.",
                    "tickers": ["ELF"],
                    "exchanges": ["NYSE"],
                    "sic": "2844",
                    "filings": {"recent": {}},
                },
            )
        return httpx.Response(
            200,
            text=(
                "<html><body>"
                "<h1>Item 2.01 Completion of Acquisition or Disposition of Assets</h1>"
                "<p>BeautyCo completed the acquisition of SkinCare Labs, a cosmetics brand.</p>"
                "<h1>Item 9.01 Financial Statements and Exhibits</h1>"
                "</body></html>"
            ),
        )

    client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )

    documents = client.fetch_transaction_signal_documents(
        sample_profile(),
        since=date(2021, 5, 4),
        ttl_hours=24,
        form_type="8-K",
        limit=50,
        company_limit=10,
        source_strength=SourceStrength.C,
        source_dimension="buyer_long_list_recall.transaction_signal",
        primary_items=["2.01"],
        supporting_items=["1.01"],
        require_primary_item=True,
        fetch_filing_text=True,
        text_scope="primary_item_section",
        as_of_date=date(2026, 5, 4),
    )

    assert any(url.startswith("https://data.sec.gov/submissions/CIK0000002000.json") for url in seen_urls)
    assert len(documents) == 1
    assert documents[0].source_type == SourceType.sec_filing
    assert documents[0].source_dimension == "buyer_long_list_recall.transaction_signal"
    assert documents[0].metadata["form"] == "8-K"
    assert documents[0].metadata["sic"] == "2844"
    assert documents[0].metadata["filing_items"] == ["2.01", "9.01"]
    assert documents[0].metadata["edgar_item_match"]["primary"] == ["2.01"]
    assert documents[0].metadata["text_scope"] == "primary_item_section"
    assert documents[0].filing_accession == "0000002000-25-000001"
    assert "BeautyCo completed the acquisition of SkinCare Labs" in (documents[0].raw_text or "")


def test_ma_history_retriever_recalls_same_and_adjacent_recent_deals(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key="news-test-key")
    strategy = DataSourceStrategy.from_settings(settings)
    edgar_source = FakeEdgarTransactionSource(edgar_transaction_documents())
    news_source = FakeNewsSource(transaction_documents())
    retriever = MAHistoryRetriever(
        strategy,
        newsapi_client=news_source,
        edgar_client=edgar_source,
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["BeautyCo", "Retail Corp"]
    by_name = {hit.candidate_name: hit for hit in result.hits}
    assert by_name["BeautyCo"].candidate_ticker == "BTY"
    assert by_name["BeautyCo"].candidate_cik == "0000002000"
    beauty_events = by_name["BeautyCo"].retrieval_metadata["deal_events"]
    retail_events = by_name["Retail Corp"].retrieval_metadata["deal_events"]
    assert beauty_events[0]["target_acquired"] == "SkinCare Labs"
    assert beauty_events[0]["sector_match"] == "same"
    assert retail_events[0]["sector_match"] == "adjacent"
    assert edgar_source.calls[0]["since"] == date(2021, 5, 4)
    assert len(news_source.queries) == 5
    assert result.metadata["primary_source"] == "edgar_8k"
    assert result.metadata["secondary_source"] == "newsapi"
    assert result.metadata["edgar_documents_checked"] == 1
    assert result.metadata["news_documents_checked"] == 25
    assert result.metadata["news_lookback_start"] == "2026-04-04"
    assert news_source.calls[0]["kwargs"]["from_date"] == "2026-04-04"
    assert result.metadata["skip_reasons"]["no_transaction_language"] == 0
    assert result.metadata["lookback_start"] == "2021-05-04"
    assert any("older than 2021-05-04" in warning for warning in result.warnings)
    assert any("unrelated transaction events" in warning for warning in result.warnings)
    assert any("without a parsed buyer" in warning for warning in result.warnings)


def test_ma_history_retriever_uses_sec_filer_identity_for_item_201_hits(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key=None)
    strategy = DataSourceStrategy.from_settings(settings)
    edgar_source = FakeEdgarTransactionSource(sec_item_201_fragment_document())
    retriever = MAHistoryRetriever(
        strategy,
        edgar_client=edgar_source,
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["EDGEWELL PERSONAL CARE Co"]
    hit = result.hits[0]
    assert hit.candidate_ticker == "EPC"
    assert hit.candidate_cik == "0001096752"
    assert "Item 2.01" not in hit.candidate_name
    assert "completed the previously" not in hit.candidate_name.casefold()
    assert hit.retrieval_metadata["deal_events"][0]["target_acquired"] == "Luxury Labs"


def test_ma_history_retriever_handles_disabled_newsapi_without_throwing(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key=None)
    strategy = DataSourceStrategy.from_settings(settings)
    news_source = FakeNewsSource(transaction_documents())
    retriever = MAHistoryRetriever(strategy, newsapi_client=news_source, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert result.hits == []
    assert news_source.queries == []
    assert any("newsapi disabled" in warning for warning in result.warnings)


def test_buyer_candidate_retriever_fanout_uses_or_recall_without_dedupe(tmp_path: Path) -> None:
    profile = sample_profile()
    evidence = sample_evidence()
    first = CandidateHit(
        candidate_name="Shared Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="FirstRetriever",
        source_path=["first_path"],
        fit_reason="First path recalled the buyer.",
        evidence=[evidence],
        confidence=0.7,
    )
    second = CandidateHit(
        candidate_name="Shared Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="SecondRetriever",
        source_path=["second_path"],
        fit_reason="Second path recalled the buyer.",
        evidence=[evidence],
        confidence=0.8,
    )
    fanout = BuyerCandidateRetriever([StaticRetriever("FirstRetriever", [first]), StaticRetriever("SecondRetriever", [second])])
    orchestrator = PipelineOrchestrator(strategic_candidate_retriever=fanout)

    result = orchestrator.retrieve_strategic_candidates(profile)

    assert [hit.retriever_name for hit in result.hits] == ["FirstRetriever", "SecondRetriever"]
    assert [hit.source_path for hit in result.hits] == [["first_path"], ["second_path"]]
    assert result.metadata["hit_count"] == 2


def test_strategic_candidates_api_returns_target_profile_and_hits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = settings_for_tests(tmp_path)
    evidence = sample_evidence()
    hit = CandidateHit(
        candidate_name="Beauty Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="SameSicRetriever",
        source_path=["same_sic"],
        fit_reason="Shares target SIC 2844.",
        evidence=[evidence],
        confidence=0.72,
    )
    monkeypatch.setattr("src.api.main.build_target_profile_extractor", lambda _settings, _session: FakeProfileService())
    monkeypatch.setattr(
        "src.api.main.build_strategic_buyer_candidate_retriever",
        lambda _settings, _session: StaticRetriever("BuyerCandidateRetriever", [hit]),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/buyers/strategic-candidates?query=ELF")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_profile"]["ticker"] == "ELF"
    assert payload["hits"][0]["candidate_name"] == "Beauty Buyer Inc."
    assert payload["metadata"]["retrieval"]["retriever"] == "BuyerCandidateRetriever"


def sample_profile() -> TargetProfile:
    return TargetProfile(
        target_id="0001600033",
        name="e.l.f. Beauty, Inc.",
        ticker="ELF",
        cik="0001600033",
        exchange="NYSE",
        sic="2844",
        business_summary="e.l.f. Beauty sells cosmetics.",
        products=["cosmetics"],
        customer_segments=["retail consumers"],
        channels=["retail"],
        geographies=["United States"],
        keywords=["beauty"],
        adjacent_categories=["personal care"],
    )


def sample_evidence() -> Evidence:
    return Evidence(
        claim="Fixture evidence.",
        source_type="sec_company_mapping",
        source_strength=SourceStrength.B,
        url="https://www.sec.gov/files/company_tickers_exchange.json",
        verified_fact=True,
    )


def same_sic_documents() -> list[SourceDocument]:
    return [
        source_document(
            {
                "canonical_name": "Beauty Buyer Inc.",
                "ticker": "BBY",
                "cik": "0000002000",
                "exchange": "NYSE",
                "sic": "2844",
                "market_cap": 5_000_000_000,
            },
            url="https://www.sec.gov/files/company_tickers_exchange.json",
        ),
        source_document(
            {
                "canonical_name": "Different SIC Inc.",
                "ticker": "DSIC",
                "cik": "0000003000",
                "sic": "9999",
            },
            url="https://www.sec.gov/files/company_tickers_exchange.json",
        ),
        source_document(
            {
                "canonical_name": "e.l.f. Beauty, Inc.",
                "ticker": "ELF",
                "cik": "0001600033",
                "sic": "2844",
            },
            url="https://www.sec.gov/files/company_tickers_exchange.json",
        ),
        source_document(
            {
                "canonical_name": "No Evidence Same SIC Inc.",
                "ticker": "NOEV",
                "cik": "0000004000",
                "sic": "2844",
            },
            url=None,
        ),
    ]


def transaction_documents() -> list[SourceDocument]:
    return [
        news_document(
            "BeautyCo acquired SkinCare Labs, a cosmetics brand.",
            "2025-06-01T10:00:00Z",
            "https://example.com/beautyco-skincare",
        ),
        news_document(
            "Retail Corp acquired Channel Labs to expand retail distribution.",
            "2024-08-15T10:00:00Z",
            "https://example.com/retail-channel",
        ),
        news_document(
            "IndustrialCo acquired Mining Labs for heavy equipment services.",
            "2025-03-01T10:00:00Z",
            "https://example.com/industrial-mining",
        ),
        news_document(
            "BeautyCo acquired Old Cosmetics Co.",
            "2021-05-03T10:00:00Z",
            "https://example.com/beautyco-old",
        ),
        news_document(
            "Acquisition of a cosmetics brand was announced by undisclosed investors.",
            "2025-04-01T10:00:00Z",
            "https://example.com/no-buyer",
        ),
    ]


def edgar_transaction_documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            source_id="edgar",
            source_dimension="buyer_long_list_recall.transaction_signal",
            source_type=SourceType.sec_filing,
            source_strength=SourceStrength.C,
            target_cik="0000002000",
            target_ticker="BTY",
            url="https://www.sec.gov/Archives/edgar/data/2000/000000200025000001/0000002000-25-000001-index.htm",
            filing_accession="0000002000-25-000001",
            raw_text="BeautyCo acquired SkinCare Labs.",
            metadata={
                "canonical_name": "BeautyCo",
                "ticker": "BTY",
                "cik": "0000002000",
                "form": "8-K",
                "filing_date": "2025-06-01",
                "sic": "2844",
                "title": "BeautyCo acquired SkinCare Labs.",
            },
            retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
    ]


def sec_item_201_fragment_document() -> list[SourceDocument]:
    return [
        SourceDocument(
            source_id="edgar",
            source_dimension="buyer_long_list_recall.transaction_signal",
            source_type=SourceType.sec_filing,
            source_strength=SourceStrength.C,
            target_cik="0001096752",
            target_ticker="EPC",
            url="https://www.sec.gov/Archives/edgar/data/1096752/000109675225000001/0001096752-25-000001-index.htm",
            filing_accession="0001096752-25-000001",
            raw_text=(
                "Item 2.01 Completion of Acquisition or Disposition of Assets. "
                "The Company completed the previously announced acquisition of Luxury Labs. "
                "The shares which were settled immediately prior to closing were cancelled."
            ),
            metadata={
                "canonical_name": "EDGEWELL PERSONAL CARE Co",
                "ticker": "EPC",
                "cik": "0001096752",
                "form": "8-K",
                "filing_date": "2025-06-01",
                "sic": "2844",
                "filing_items": ["2.01", "9.01"],
                "edgar_item_match": {
                    "primary": ["2.01"],
                    "supporting": [],
                    "basis": "sec_submissions_recent.items",
                },
                "title": "8-K Item 2.01 - EDGEWELL PERSONAL CARE Co",
            },
            retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
    ]


def source_document(metadata: dict[str, Any], url: str | None) -> SourceDocument:
    return SourceDocument(
        source_id="edgar",
        source_dimension="buyer_long_list_recall.public_company_peer_discovery",
        source_type=SourceType.sec_company_mapping,
        source_strength=SourceStrength.B,
        target_cik=metadata.get("cik"),
        target_ticker=metadata.get("ticker"),
        url=url,
        metadata=metadata,
        retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
    )


def news_document(title: str, published_at: str, url: str) -> SourceDocument:
    return SourceDocument(
        source_id="newsapi",
        source_dimension="buyer_long_list_recall.transaction_news",
        source_type=SourceType.news_article,
        source_strength=SourceStrength.B,
        target_cik="0001600033",
        target_ticker="ELF",
        url=url,
        metadata={
            "title": title,
            "description": title,
            "publishedAt": published_at,
            "source": {"name": "Example News"},
        },
        retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
    )


class FakeSicSource:
    def __init__(self, documents: list[SourceDocument]) -> None:
        self.documents = documents

    def list_public_company_profiles_by_sic(self, *_args, **_kwargs) -> list[SourceDocument]:
        return self.documents


class FakeNewsSource:
    def __init__(self, documents: list[SourceDocument]) -> None:
        self.documents = documents
        self.queries: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def fetch_articles_for_query(self, query: str, *_args, **_kwargs) -> list[SourceDocument]:
        self.queries.append(query)
        self.calls.append({"query": query, "kwargs": _kwargs})
        return self.documents


class FakeEdgarTransactionSource:
    def __init__(self, documents: list[SourceDocument]) -> None:
        self.documents = documents
        self.calls: list[dict[str, Any]] = []

    def fetch_transaction_signal_documents(self, _profile: TargetProfile, since: date, *_args, **_kwargs) -> list[SourceDocument]:
        self.calls.append({"since": since, "kwargs": _kwargs})
        return self.documents


class StaticRetriever:
    def __init__(self, name: str, hits: list[CandidateHit]) -> None:
        self.name = name
        self.hits = hits

    def retrieve_with_context(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})

    def retrieve(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})


class FakeProfileService:
    def build_profile(self, _query: str):
        class ProfileResult:
            target_profile = sample_profile()
            warnings: list[str] = []
            extraction_metadata = {"extractor_version": "test"}

        return ProfileResult()
