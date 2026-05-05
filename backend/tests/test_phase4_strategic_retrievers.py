from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from fastapi.testclient import TestClient
from sqlalchemy import update

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
from src.repositories.buyer_recall_cache import BuyerRecallCache
from src.repositories.database import create_session_factory, init_db
from src.repositories.models import BuyerRecallCacheRecord
from src.retrievers import BuyerCandidateRetriever, MAHistoryRetriever, SameSicRetriever, StrategicAcquisitionIntentRetriever
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "retrieval_rules_path": Path("config/retrieval_rules.yaml"),
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
    assert hit.data_source == ["edgar"]
    assert hit.retrieval_metadata["target_sic"] == "2844"
    assert hit.retrieval_metadata["candidate_sic"] == "2844"
    assert hit.evidence[0].source_dimension == "buyer_long_list_recall.public_company_peer_discovery"
    assert all("polygon disabled" not in warning for warning in result.warnings)
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
    assert by_name["BeautyCo"].data_source == ["edgar", "newsapi"]
    assert by_name["Retail Corp"].data_source == ["newsapi"]
    beauty_events = by_name["BeautyCo"].retrieval_metadata["deal_events"]
    retail_events = by_name["Retail Corp"].retrieval_metadata["deal_events"]
    assert beauty_events[0]["target_acquired"] == "SkinCare Labs"
    assert beauty_events[0]["sector_match"] == "same"
    assert retail_events[0]["sector_match"] == "adjacent"
    assert edgar_source.calls[0]["since"] == date(2021, 5, 4)
    assert len(news_source.queries) == 5
    assert news_source.queries[0].startswith('"cosmetics"')
    assert news_source.queries[-1].startswith('"personal care"')
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


def test_ma_history_retriever_recalls_google_rss_resolved_and_pending_buyers(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key=None)
    strategy = DataSourceStrategy.from_settings(settings)
    google_source = FakeGoogleRssSource(
        search_documents=[
            rss_document(
                "Clean Cosmetics Lab sold to Ulta Beauty, expanding cosmetics brands.",
                "2026-04-20T10:00:00+00:00",
                "https://news.google.com/rss/articles/ulta-cosmetics",
            ),
            rss_document(
                "Private Buyer acquired Indie Beauty Co, a cosmetics company.",
                "2026-04-22T10:00:00+00:00",
                "https://news.google.com/rss/articles/private-buyer",
            ),
            rss_document(
                "Cosmetics customer acquisition costs rose during the holiday quarter.",
                "2026-04-23T10:00:00+00:00",
                "https://news.google.com/rss/articles/customer-acquisition-costs",
            ),
        ],
        topic_documents=[
            rss_document(
                "L'Oreal acquired Skin Care Co in cosmetics expansion.",
                "2026-04-21T10:00:00+00:00",
                "https://news.google.com/rss/articles/loreal-cosmetics",
            ),
            rss_document(
                "Mega Corp acquired Mining Labs for equipment services.",
                "2026-04-21T10:00:00+00:00",
                "https://news.google.com/rss/articles/mining",
            ),
        ],
    )
    identity_resolver = FakeBuyerIdentityResolver(
        {
            "Ulta Beauty": [resolved_target("Ulta Beauty, Inc.", "ULTA", "0001403568")],
            "L'Oreal": [resolved_target("L'Oreal S.A.", "LRLCY", "0000007777")],
        }
    )
    llm_client = FakeMnaLlmClient(
        {
            "ulta-cosmetics": {
                "is_mna": True,
                "buyer_name": "Ulta Beauty",
                "acquired_target": "Clean Cosmetics Lab",
                "deal_type": "acquisition",
                "confidence": 0.93,
                "rationale": "The title says Clean Cosmetics Lab sold to Ulta Beauty.",
            },
            "private-buyer": {
                "is_mna": True,
                "buyer_name": "Private Buyer",
                "acquired_target": "Indie Beauty Co",
                "deal_type": "acquisition",
                "confidence": 0.91,
                "rationale": "The title says Private Buyer acquired Indie Beauty Co.",
            },
            "loreal-cosmetics": {
                "is_mna": True,
                "buyer_name": "L'Oreal",
                "acquired_target": "Skin Care Co",
                "deal_type": "acquisition",
                "confidence": 0.95,
                "rationale": "The title says L'Oreal acquired Skin Care Co.",
            },
            "customer-acquisition-costs": {
                "is_mna": False,
                "buyer_name": None,
                "acquired_target": None,
                "deal_type": None,
                "confidence": 0.98,
                "rationale": "Customer acquisition costs are not M&A.",
            },
        }
    )
    retriever = MAHistoryRetriever(
        strategy,
        edgar_client=FakeEdgarTransactionSource([]),
        google_news_rss_client=google_source,
        buyer_identity_resolver=identity_resolver,
        llm_client=llm_client,
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    by_name = {hit.candidate_name: hit for hit in result.hits}
    assert set(by_name) == {"L'Oreal S.A.", "Private Buyer", "Ulta Beauty, Inc."}
    assert google_source.topic_calls == ["BUSINESS"]
    assert len(google_source.search_queries) == 5
    assert google_source.search_queries[0].startswith('"Perfumes, cosmetics, and other toilet preparations"')
    assert "acquisition OR acquired" in google_source.search_queries[0]
    assert by_name["Ulta Beauty, Inc."].candidate_ticker == "ULTA"
    assert by_name["Ulta Beauty, Inc."].candidate_cik == "0001403568"
    assert by_name["L'Oreal S.A."].candidate_ticker == "LRLCY"
    assert by_name["Private Buyer"].candidate_ticker is None
    assert by_name["Private Buyer"].candidate_cik is None
    assert by_name["Private Buyer"].pending_verification is True
    assert by_name["Private Buyer"].confidence == 0.52
    assert by_name["Ulta Beauty, Inc."].data_source == ["google_news_rss"]
    assert by_name["Private Buyer"].retrieval_metadata["identity_resolution"][0]["status"] == "unresolved"
    assert by_name["Ulta Beauty, Inc."].retrieval_metadata["deal_events"][0]["target_acquired"] == "Clean Cosmetics Lab"
    assert result.metadata["rss_topic"] == "BUSINESS"
    assert result.metadata["rss_search_documents_checked"] == 15
    assert result.metadata["rss_topic_documents_checked"] == 1
    assert result.metadata["rss_topic_documents_filtered"] == 1
    assert result.metadata["rss_llm_mna_count"] == 3
    assert result.metadata["rss_llm_non_mna_count"] == 1
    assert result.metadata["rss_identity_resolved"] == 2
    assert result.metadata["rss_identity_unresolved"] == 1
    assert all("acquisition, acquired, acquires" in prompt for prompt in llm_client.prompts)
    assert "customer acquisition costs" not in by_name


def test_ma_history_retriever_skips_google_rss_when_required_llm_unavailable(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key=None)
    strategy = DataSourceStrategy.from_settings(settings)
    google_source = FakeGoogleRssSource(
        search_documents=[
            rss_document(
                "Ulta Beauty acquired Clean Cosmetics Lab, a cosmetics brand.",
                "2026-04-20T10:00:00+00:00",
                "https://news.google.com/rss/articles/ulta-cosmetics",
            )
        ],
        topic_documents=[],
    )
    retriever = MAHistoryRetriever(
        strategy,
        edgar_client=FakeEdgarTransactionSource([]),
        google_news_rss_client=google_source,
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert result.hits == []
    assert result.metadata["rss_llm_error_count"] == 1
    assert result.metadata["rss_llm_skipped_count"] == 1
    assert any("LLM extraction unavailable" in warning for warning in result.warnings)


def test_ma_history_retriever_handles_disabled_newsapi_without_throwing(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, news_api_key=None)
    strategy = DataSourceStrategy.from_settings(settings)
    news_source = FakeNewsSource(transaction_documents())
    retriever = MAHistoryRetriever(strategy, newsapi_client=news_source, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert result.hits == []
    assert news_source.queries == []
    assert any("newsapi disabled" in warning for warning in result.warnings)


def test_strategic_acquisition_intent_retriever_recalls_llm_web_search_intent_with_evidence_cap(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    web_search = FakeStrategicWebSearchJSONClient(
        {
            "target_company": "e.l.f. Beauty, Inc.",
            "target_sic": "2844",
            "potential_buyers": [
                {
                    "company_name": "e.l.f. Beauty, Inc.",
                    "ticker": "ELF",
                    "sector_relevance": "same",
                    "evidence": [
                        {
                            "evidence_summary": "The seller itself discussed strategic acquisitions.",
                            "source_url": "https://investor.elfbeauty.com/self",
                            "sector_relevance": "same",
                        }
                    ],
                },
                {
                    "company_name": "Ulta Beauty, Inc.",
                    "ticker": "ULTA",
                    "cik": "0001403568",
                    "domain": "https://www.ulta.com/",
                    "sector_relevance": "Same-industry: beauty retail and cosmetics",
                    "fit_reason": "Ulta has sourced strategic acquisition appetite in beauty and personal care.",
                    "evidence": [
                        {
                            "evidence_summary": "Ulta said acquisitions are part of its beauty growth strategy.",
                            "source_url": "https://example.com/ulta-strategy",
                            "source_title": "Ulta outlines acquisition strategy",
                            "quote_or_snippet": "Acquisitions are part of Ulta's beauty growth strategy.",
                            "intent_type": "strategic_acquisitions",
                            "sector_relevance": "same",
                        },
                        {
                            "evidence_summary": "Ulta corporate development page highlights acquisition opportunities.",
                            "source_url": "https://example.com/ulta-corp-dev",
                            "source_title": "Corporate development",
                            "intent_type": "corporate_development",
                            "sector_relevance": "same",
                        },
                        {
                            "evidence_summary": "An investor presentation mentions tuck-in acquisitions.",
                            "source_url": "https://example.com/ulta-investor-day",
                            "source_title": "Investor day",
                            "intent_type": "tuck_in_acquisitions",
                            "sector_relevance": "same",
                        },
                    ],
                },
                {
                    "company_name": "Industrial Buyer Co.",
                    "sector_relevance": "unrelated",
                    "evidence": [
                        {
                            "evidence_summary": "Industrial Buyer wants equipment acquisitions.",
                            "source_url": "https://example.com/industrial",
                            "sector_relevance": "unrelated",
                        }
                    ],
                },
                {
                    "company_name": "No Evidence Buyer",
                    "sector_relevance": "same",
                    "evidence": [{"evidence_summary": "No URL is available.", "sector_relevance": "same"}],
                },
            ],
        }
    )
    retriever = StrategicAcquisitionIntentRetriever(strategy, web_search_client=web_search, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Ulta Beauty, Inc."]
    hit = result.hits[0]
    assert hit.buyer_type == BuyerType.strategic
    assert hit.candidate_ticker == "ULTA"
    assert hit.candidate_cik == "0001403568"
    assert hit.candidate_domain == "www.ulta.com"
    assert hit.source_path == ["strategic_acquisition_intent_llm_web_search"]
    assert hit.data_source == ["llm_web_search"]
    assert hit.pending_verification is True
    assert hit.confidence == 0.57
    assert len(hit.evidence) == 2
    assert all(evidence.verified_fact is False for evidence in hit.evidence)
    assert hit.evidence[0].source_dimension == "buyer_long_list_recall.strategic_acquisition_intent"
    assert hit.evidence[0].source_type == "llm_web_search"
    assert hit.retrieval_metadata["sector_relevance"] == "same"
    assert hit.retrieval_metadata["llm_web_search_evidence_count"] == 2
    assert result.metadata["candidate_records_available_before_cap"] == 1
    assert result.metadata["evidence_available_before_cap"] == 3
    assert result.metadata["evidence_truncated"] == 1
    assert result.metadata["skip_reasons"]["self_candidate"] == 2
    assert result.metadata["skip_reasons"]["unrelated_sector"] == 2
    assert result.metadata["skip_reasons"]["missing_evidence"] == 2
    assert result.metadata["skip_reasons"]["duplicate_candidate"] == 1
    assert result.metadata["llm_web_search_attempts"] == 2
    assert result.metadata["min_candidates_before_retry"] == 5
    assert web_search.source_business_types == [
        "strategic_buyer_acquisition_intent_web_search",
        "strategic_buyer_acquisition_intent_web_search",
    ]
    assert web_search.calls[0]["max_results"] == 10
    assert web_search.calls[0]["max_total_results"] == 40
    assert web_search.calls[0]["search_context_size"] == "low"
    assert web_search.calls[0]["fetch_max_uses"] == 5
    assert web_search.calls[0]["fetch_max_content_tokens"] == 12000
    assert "use exactly same, adjacent, or unrelated" in web_search.calls[0]["system_prompt"]
    assert web_search.calls[0]["json_schema"]["properties"]["candidates"]["maxItems"] == 25
    assert web_search.calls[0]["json_schema"]["properties"]["candidates"]["items"]["properties"]["evidence"]["maxItems"] == 2
    assert "Use web search" in web_search.prompts[0]
    assert "e.l.f. Beauty, Inc." not in web_search.prompts[0]
    assert "ELF" not in web_search.prompts[0]
    assert "e.l.f. Cosmetics" not in web_search.prompts[0]
    assert "Same-industry terms" in web_search.prompts[0]
    assert "cosmetics" in web_search.prompts[0]
    assert "SIC=2844" in web_search.prompts[0]
    assert "from 2025-05-04 through 2026-05-04" in web_search.prompts[0]
    assert "Adjacent-industry terms" in web_search.prompts[0]
    assert "Enumerate qualifying companies from SEC filings, investor-relations decks" in web_search.prompts[0]
    assert "up to 25" in web_search.prompts[0]
    assert "do not stop after finding the first valid example" in web_search.prompts[0]
    assert "at most 2 evidence" in web_search.prompts[0]
    assert "low-recall retry" in web_search.prompts[1]


def test_strategic_acquisition_intent_retriever_tolerates_llm_type_drift(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    web_search = FakeStrategicWebSearchJSONClient(
        {
            "target_company": "e.l.f. Beauty, Inc.",
            "target_sic": 2844,
            "candidates": [
                {
                    "company_name": "Beauty Strategy Co.",
                    "ticker": "BSC",
                    "sector_relevance": "same",
                    "fit_reason": "The company has sourced acquisition appetite in beauty.",
                    "has_strategic_intent": None,
                    "evidence": [
                        {
                            "evidence_summary": "Beauty Strategy Co. is pursuing acquisitions in cosmetics.",
                            "source_url": "https://example.com/beauty-strategy-acquisitions",
                            "sector_relevance": "same",
                            "intent_type": "strategic_acquisitions",
                            "is_relevant": None,
                        }
                    ],
                }
            ],
        }
    )
    retriever = StrategicAcquisitionIntentRetriever(strategy, web_search_client=web_search, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Beauty Strategy Co."]
    assert result.metadata["target_sic"] == "2844"
    assert result.metadata["llm_web_search_candidates_checked"] == 2
    assert result.metadata["llm_web_search_attempts"] == 2
    assert not any("LLM web search failed" in warning for warning in result.warnings)


def test_strategic_acquisition_intent_retriever_retries_low_recall_and_merges_term_matched_candidates(
    tmp_path: Path,
) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    retry_candidates = [
        {
            "company_name": f"Broad Beauty Buyer {index}",
            "ticker": f"BBB{index}",
            "fit_reason": "The company describes an active corporate development strategy for cosmetics acquisitions.",
            "evidence": [
                {
                    "evidence_summary": "Management said it is seeking acquisitions across cosmetics and personal care.",
                    "source_url": f"https://example.com/broad-beauty-buyer-{index}",
                    "source_title": "Corporate development strategy",
                }
            ],
        }
        for index in range(1, 5)
    ]
    web_search = FakeStrategicWebSearchJSONClient(
        [
            {
                "target_sic": "2844",
                "candidates": [
                    {
                        "company_name": "Initial Beauty Buyer",
                        "sector_relevance": "same",
                        "fit_reason": "Initial buyer has sourced acquisition intent in beauty products.",
                        "evidence": [
                            {
                                "evidence_summary": "Initial buyer is pursuing beauty products acquisitions.",
                                "source_url": "https://example.com/initial-beauty-buyer",
                                "sector_relevance": "same",
                            }
                        ],
                    }
                ],
            },
            {"target_sic": "2844", "candidates": retry_candidates},
        ]
    )
    retriever = StrategicAcquisitionIntentRetriever(strategy, web_search_client=web_search, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert {hit.candidate_name for hit in result.hits} == {
        "Initial Beauty Buyer",
        "Broad Beauty Buyer 1",
        "Broad Beauty Buyer 2",
        "Broad Beauty Buyer 3",
        "Broad Beauty Buyer 4",
    }
    assert len(web_search.prompts) == 2
    assert "low-recall retry" in web_search.prompts[1]
    assert result.metadata["llm_web_search_candidates_checked"] == 5
    assert result.metadata["candidate_records_available_before_cap"] == 5
    assert result.metadata["skip_reasons"] == {}


def test_strategic_acquisition_intent_retriever_infers_relevance_from_reason_when_llm_uses_scores(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    web_search = FakeStrategicWebSearchJSONClient(
        {
            "target_company": "e.l.f. Beauty, Inc.",
            "target_sic": 2844,
            "candidates": [
                {
                    "company_name": "L'Oreal S.A.",
                    "ticker": "OR.PA",
                    "sector_relevance": "High",
                    "fit_reason": (
                        "Acquiring a premium skincare brand directly adjacent to SIC 2844 personal care "
                        "and beauty accessories."
                    ),
                    "has_strategic_intent": True,
                    "evidence": [
                        {
                            "evidence_summary": "L'Oreal announced an agreement to acquire a premium skincare brand.",
                            "source_url": "https://www.loreal-finance.com/eng/press-release/loreal-groupe-acquire-majority-stake-medik8",
                            "sector_relevance": "High",
                            "intent_type": "acquire",
                            "is_relevant": True,
                        }
                    ],
                },
                {
                    "company_name": "e.l.f. Beauty, Inc.",
                    "ticker": "ELF",
                    "sector_relevance": "High",
                    "fit_reason": "Strategic acquisition of a skin-focused beauty brand adjacent to personal care.",
                    "has_strategic_intent": True,
                    "evidence": [
                        {
                            "evidence_summary": "e.l.f. Beauty announced an acquisition.",
                            "source_url": "https://www.nasdaq.com/press-release/elf-beauty-announces-definitive-agreement-acquire-rhode",
                            "sector_relevance": "High",
                            "intent_type": "acquisition",
                            "is_relevant": True,
                        }
                    ],
                },
            ],
        }
    )
    retriever = StrategicAcquisitionIntentRetriever(strategy, web_search_client=web_search, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["L'Oreal S.A."]
    assert result.hits[0].retrieval_metadata["sector_relevance"] == "adjacent"
    assert result.metadata["skip_reasons"]["self_candidate"] == 2
    assert result.metadata["skip_reasons"]["duplicate_candidate"] == 1
    assert "unrelated_sector" not in result.metadata["skip_reasons"]


def test_strategic_acquisition_intent_retriever_enforces_candidate_cap(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    strategy = DataSourceStrategy.from_settings(settings)
    web_search = FakeStrategicWebSearchJSONClient(
        {
            "target_company": "e.l.f. Beauty, Inc.",
            "target_sic": "2844",
            "candidates": [
                {
                    "company_name": f"Strategic Buyer {index:02d}",
                    "sector_relevance": "same",
                    "fit_reason": "The company has relevant strategic acquisition intent.",
                    "evidence": [
                        {
                            "evidence_summary": f"Strategic Buyer {index:02d} is seeking cosmetics acquisitions.",
                            "source_url": f"https://example.com/buyer-{index:02d}",
                            "sector_relevance": "same",
                            "intent_type": "strategic_acquisitions",
                        }
                    ],
                }
                for index in range(30)
            ],
        }
    )
    retriever = StrategicAcquisitionIntentRetriever(strategy, web_search_client=web_search, as_of_date=date(2026, 5, 4))

    result = retriever.retrieve_with_context(sample_profile())

    assert len(result.hits) == 25
    assert result.metadata["candidate_records_available_before_cap"] == 30
    assert result.metadata["candidate_records_truncated"] == 5
    assert result.metadata["evidence_used"] == 25
    assert len(web_search.prompts) == 1


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


def test_buyer_candidate_retriever_caches_stage_result_for_same_seller(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    hit = CandidateHit(
        candidate_name="Beauty Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="CountingRetriever",
        source_path=["same_sic"],
        fit_reason="Shares target SIC 2844.",
        evidence=[sample_evidence()],
        confidence=0.72,
    )
    retriever = CountingRetriever("CountingRetriever", [hit])

    with session_factory() as session:
        fanout = BuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache",
        )
        first = fanout.retrieve(sample_profile())

    with session_factory() as session:
        fanout = BuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache",
        )
        second = fanout.retrieve(sample_profile())

    assert retriever.calls == 1
    assert [hit.candidate_name for hit in second.hits] == ["Beauty Buyer Inc."]
    assert first.metadata["cache"]["status"] == "miss"
    assert first.metadata["cache"]["target_ticker"] == "ELF"
    assert second.metadata["cache"]["status"] == "hit"
    assert second.metadata["cache"]["stage_name"] == "strategic_buyer_recall"


def test_buyer_candidate_retriever_recomputes_after_cache_ttl_expiry(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    hit = CandidateHit(
        candidate_name="Beauty Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="CountingRetriever",
        source_path=["same_sic"],
        fit_reason="Shares target SIC 2844.",
        evidence=[sample_evidence()],
        confidence=0.72,
    )
    retriever = CountingRetriever("CountingRetriever", [hit])

    with session_factory() as session:
        fanout = BuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache-expiry",
        )
        fanout.retrieve(sample_profile())

    with session_factory() as session:
        session.execute(update(BuyerRecallCacheRecord).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        session.commit()

    with session_factory() as session:
        fanout = BuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache-expiry",
        )
        result = fanout.retrieve(sample_profile())

    assert retriever.calls == 2
    assert result.metadata["cache"]["status"] == "miss"


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
        data_source=["edgar"],
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
    assert payload["hits"][0]["data_source"] == ["edgar"]
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


def rss_document(title: str, published_at: str, url: str) -> SourceDocument:
    return SourceDocument(
        source_id="google_news_rss",
        source_dimension="buyer_long_list_recall.transaction_news",
        source_type=SourceType.news_article,
        source_strength=SourceStrength.C,
        target_cik="0001600033",
        target_ticker="ELF",
        url=url,
        metadata={
            "title": title,
            "description": title,
            "publishedAt": published_at,
            "source": {"name": "Example News"},
        },
        raw_text=title,
        retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
    )


def resolved_target(name: str, ticker: str, cik: str) -> Any:
    from src.domain import ResolvedTarget

    return ResolvedTarget(
        canonical_name=name,
        ticker=ticker,
        cik=cik,
        resolution_confidence=0.95,
        matched_input=name,
        source_provenance=["test"],
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


class FakeGoogleRssSource:
    def __init__(self, search_documents: list[SourceDocument], topic_documents: list[SourceDocument]) -> None:
        self.search_documents = search_documents
        self.topic_documents = topic_documents
        self.search_queries: list[str] = []
        self.topic_calls: list[str] = []

    def fetch_articles_for_query(self, query: str, *_args, **_kwargs) -> list[SourceDocument]:
        self.search_queries.append(query)
        return self.search_documents

    def fetch_topic_articles(self, topic: str, *_args, **_kwargs) -> list[SourceDocument]:
        self.topic_calls.append(topic)
        return self.topic_documents


class FakeMnaLlmClient:
    def __init__(self, outputs_by_url_fragment: dict[str, dict[str, Any]]) -> None:
        self.outputs_by_url_fragment = outputs_by_url_fragment
        self.prompts: list[str] = []
        self.schema_names: list[str] = []
        self.source_business_types: list[str] = []

    def generate_json(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.schema_names.append(schema_name)
        self.source_business_types.append(source_business_type)
        for url_fragment, payload in self.outputs_by_url_fragment.items():
            if url_fragment in prompt:
                return payload
        return {
            "is_mna": False,
            "buyer_name": None,
            "acquired_target": None,
            "deal_type": None,
            "confidence": 0.0,
            "rationale": "No fixture matched.",
        }


class FakeStrategicWebSearchJSONClient:
    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.prompts: list[str] = []
        self.source_business_types: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def generate_json_with_web_search(
        self,
        prompt: str,
        _schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_results: int = 5,
        max_total_results: int = 5,
        search_engine: str = "auto",
        search_context_size: str = "low",
        fetch_engine: str = "auto",
        fetch_max_uses: int | None = None,
        fetch_max_content_tokens: int | None = None,
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.source_business_types.append(source_business_type)
        self.calls.append(
            {
                "json_schema": json_schema,
                "system_prompt": system_prompt,
                "max_results": max_results,
                "max_total_results": max_total_results,
                "search_engine": search_engine,
                "search_context_size": search_context_size,
                "fetch_engine": fetch_engine,
                "fetch_max_uses": fetch_max_uses,
                "fetch_max_content_tokens": fetch_max_content_tokens,
            }
        )
        return self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]


class FakeBuyerIdentityResolver:
    def __init__(self, matches_by_query: dict[str, list[Any]]) -> None:
        self.matches_by_query = matches_by_query
        self.queries: list[str] = []

    def resolve_by_company_name(self, company_name: str) -> list[Any]:
        self.queries.append(company_name)
        return self.matches_by_query.get(company_name, [])


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


class CountingRetriever(StaticRetriever):
    def __init__(self, name: str, hits: list[CandidateHit]) -> None:
        super().__init__(name, hits)
        self.calls = 0

    def retrieve_with_context(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        self.calls += 1
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})


class FakeProfileService:
    def build_profile(self, _query: str):
        class ProfileResult:
            target_profile = sample_profile()
            warnings: list[str] = []
            extraction_metadata = {"extractor_version": "test"}

        return ProfileResult()
