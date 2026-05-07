from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from src.config import Settings
from src.domain import ResolvedTarget, SourceStrength, SourceType, TargetProfile
from src.sources.sec import SecEdgarClient


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "retrieval_rules_path": Path("config/retrieval_rules.yaml"),
        "edgar_identity": "buyer-universe-generator/0.1 contact@example.com",
        "openrouter_api_key": "openrouter-test-key",
        "polygon_api_key": None,
        "news_api_key": None,
        "fmp_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_retrieval_policy_is_owned_outside_sources() -> None:
    from src.retrieval.policy import DataSourceStrategy
    from src.sources.strategy import DataSourceStrategy as CompatibilityStrategy

    assert DataSourceStrategy.__module__ == "src.retrieval.policy"
    assert CompatibilityStrategy is DataSourceStrategy


def test_source_document_materialization_is_owned_outside_source_base() -> None:
    from src.retrieval.materialization import source_document_from_raw_record
    from src.sources.base import source_document_from_raw_record as compatibility_helper

    assert source_document_from_raw_record.__module__ == "src.retrieval.materialization"
    assert compatibility_helper is source_document_from_raw_record


def test_investor_relations_discovery_is_owned_outside_sources() -> None:
    from src.pipelines.investor_relations import InvestorRelationsPageDiscovery
    from src.sources.investor_relations import InvestorRelationsPageDiscovery as CompatibilityDiscovery

    assert InvestorRelationsPageDiscovery.__module__ == "src.pipelines.investor_relations"
    assert CompatibilityDiscovery is InvestorRelationsPageDiscovery


def test_sec_adapter_exposes_submission_filing_entries_without_acquisition_recall(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
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
                            "form": ["8-K"],
                            "filingDate": ["2025-04-01"],
                            "reportDate": ["2025-03-31"],
                            "accessionNumber": ["0000002000-25-000001"],
                            "items": ["2.01 9.01"],
                            "primaryDocument": ["bty-20250401.htm"],
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected request: {url}")

    client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )
    target = ResolvedTarget(
        canonical_name="BeautyCo",
        ticker="BTY",
        cik="0000002000",
        exchange="NYSE",
        sic="2844",
        resolution_confidence=0.95,
        matched_input="BTY",
        source_provenance=["test"],
    )

    entries = client.list_submission_filing_entries(
        target,
        since=date(2021, 5, 4),
        as_of_date=date(2026, 5, 4),
        form_type="8-K",
        required_items=["2.01"],
        supporting_items=["1.01"],
        require_required_item=True,
    )

    assert not hasattr(SecEdgarClient, "fetch_transaction_signal_documents")
    assert len(entries) == 1
    assert entries[0].filing.accession_number == "0000002000-25-000001"
    assert entries[0].metadata["edgar_item_match"]["required"] == ["2.01"]
    assert entries[0].metadata["provider_method"] == "sec_submissions_recent_item_filter"
    assert "industry_match_basis" not in entries[0].metadata


def test_sec_transaction_signal_service_owns_same_sic_acquisition_recall(tmp_path: Path) -> None:
    from src.retrievers.strategic.sec_transaction_signals import SecTransactionSignalSource

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
                            "form": ["8-K"],
                            "filingDate": ["2025-04-01"],
                            "reportDate": ["2025-03-31"],
                            "accessionNumber": ["0000002000-25-000001"],
                            "items": ["2.01 9.01"],
                            "primaryDocument": ["bty-20250401.htm"],
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

    sec_client = SecEdgarClient(
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        use_edgartools=False,
    )
    transaction_source = SecTransactionSignalSource(sec_client)

    documents = transaction_source.fetch_transaction_signal_documents(
        sample_profile(),
        since=date(2021, 5, 4),
        ttl_hours=24,
        form_type="8-K",
        limit=50,
        company_limit=10,
        source_strength=SourceStrength.C,
        source_dimension="potential_buyer_discovery.transaction_signal",
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
    assert documents[0].source_dimension == "potential_buyer_discovery.transaction_signal"
    assert documents[0].metadata["industry_match_basis"] == "same_sic_filer"
    assert documents[0].metadata["edgar_item_match"]["primary"] == ["2.01"]
    assert documents[0].metadata["text_scope"] == "primary_item_section"
    assert "BeautyCo completed the acquisition of SkinCare Labs" in (documents[0].raw_text or "")


def test_target_profile_filing_text_selection_is_owned_outside_sources() -> None:
    from src.pipelines.sec_filing_text import source_document_for_filing_text_scope

    assert source_document_for_filing_text_scope.__module__ == "src.pipelines.sec_filing_text"
    assert hasattr(SecEdgarClient, "fetch_structured_filing_text")


def sample_profile() -> TargetProfile:
    return TargetProfile(
        target_id="0001600033",
        name="e.l.f. Beauty, Inc.",
        ticker="ELF",
        cik="0001600033",
        exchange="NYSE",
        sic="2844",
        business_summary="Beauty products",
        products=["cosmetics"],
        customer_segments=["retail"],
        channels=["retail"],
        geographies=["US"],
        keywords=["cosmetics"],
    )
