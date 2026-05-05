from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.config import Settings
from src.domain import BuyerType, CandidateHit, Evidence, SourceDocument, SourceStrength, SourceType, StrategicRetrievalResult, TargetProfile
from src.retrievers import FinancialBuyerCandidateRetriever, PEDealActivityRetriever
from src.retrievers.financial.seed_universe import PESeedFirm, PESeedUniverse
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "datasource_policy_path": Path("config/datasources.yaml"),
        "keyword_taxonomy_path": Path("config/keyword_taxonomy.yaml"),
        "pe_seed_universe_path": Path("config/pe_seed_universe.yaml"),
        "edgar_identity": "buyer-universe-generator/0.1 contact@example.com",
        "polygon_api_key": None,
        "news_api_key": None,
        "fmp_api_key": "fmp-test-key",
        "openrouter_api_key": "openrouter-test-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_data_source_policy_configures_pe_deal_activity_retriever(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))

    sponsor_source = strategy.selected_source("buyer_recall_financial_sponsors", "fmp", include_disabled=True)
    policy = strategy.retriever_config("PEDealActivityRetriever")

    assert sponsor_source is not None
    assert sponsor_source.dimension_id == "buyer_long_list_recall.financial_sponsor_activity"
    assert sponsor_source.source_strength == SourceStrength.B
    assert policy is not None
    assert policy.use_case == "buyer_recall_financial_sponsors"
    assert policy.source_roles["deal_activity_source"] == "fmp"
    assert policy.source_priority == ["fmp"]
    assert policy.lookback_years == 5
    assert policy.max_queries == 5
    assert policy.eligible_sector_matches == ["same", "adjacent"]


def test_pe_deal_activity_retriever_recalls_seeded_pe_with_recent_industry_deals(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))
    fmp_source = FakeFmpDealSource(
        {
            "cosmetics": [
                [
                    fmp_deal("deal-1", "Bain Capital", "Cosmetics Labs", "2025-06-01"),
                    fmp_deal("deal-1", "Bain Capital", "Cosmetics Labs", "2025-06-01"),
                    fmp_deal("deal-2", "Strategic Corp", "Cosmetics Labs", "2025-06-01"),
                    fmp_deal("deal-3", "Bain Capital", "Mining Labs", "2025-06-01"),
                    fmp_deal("deal-4", "Bain Capital", "Old Cosmetics Co", "2021-05-03"),
                    fmp_deal("deal-5", "KKR", "Retail Channel Labs", "2025-07-01", raw_text="KKR acquired Retail Channel Labs."),
                ]
            ]
        }
    )
    retriever = PEDealActivityRetriever(
        strategy,
        fmp_client=fmp_source,
        seed_universe=seed_universe(),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Bain Capital", "KKR"]
    by_name = {hit.candidate_name: hit for hit in result.hits}
    bain = by_name["Bain Capital"]
    assert bain.buyer_type == BuyerType.financial
    assert bain.candidate_domain == "baincapital.com"
    assert bain.source_path == ["pe_deal_activity"]
    assert bain.confidence == 0.76
    assert bain.evidence[0].source_dimension == "buyer_long_list_recall.financial_sponsor_activity"
    assert bain.evidence[0].source_strength == SourceStrength.B
    assert bain.retrieval_metadata["matched_seed_firm"] == "Bain Capital"
    assert bain.retrieval_metadata["same_sector_event_count"] == 1
    assert bain.retrieval_metadata["deal_events"][0]["target_acquired"] == "Cosmetics Labs"
    assert bain.retrieval_metadata["deal_events"][0]["sector_match"] == "same"
    assert by_name["KKR"].retrieval_metadata["adjacent_sector_event_count"] == 1
    assert by_name["KKR"].retrieval_metadata["deal_events"][0]["sector_match"] == "adjacent"
    assert result.metadata["lookback_start"] == "2021-05-04"
    assert result.metadata["query_terms"][:2] == ["Perfumes, cosmetics, and other toilet preparations", "cosmetics"]
    assert result.metadata["documents_checked"] == 5
    assert fmp_source.calls[1]["name"] == "cosmetics"
    assert fmp_source.calls[1]["ttl_hours"] == 24
    assert fmp_source.calls[1]["source_dimension"] == "buyer_long_list_recall.financial_sponsor_activity"
    assert any("older than 2021-05-04" in warning for warning in result.warnings)
    assert any("non-seed acquirer" in warning for warning in result.warnings)
    assert any("unrelated FMP deal" in warning for warning in result.warnings)


def test_pe_deal_activity_retriever_skips_disabled_fmp_without_calling_source(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path, fmp_api_key=None))
    fmp_source = FakeFmpDealSource({"cosmetics": [[fmp_deal("deal-1", "Bain Capital", "Cosmetics Labs", "2025-06-01")]]})
    retriever = PEDealActivityRetriever(
        strategy,
        fmp_client=fmp_source,
        seed_universe=seed_universe(),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert result.hits == []
    assert fmp_source.calls == []
    assert any("fmp disabled: missing BUG_FMP_API_KEY" in warning for warning in result.warnings)


def test_financial_buyer_candidate_retriever_fanout_preserves_financial_hits() -> None:
    hit = financial_hit()
    fanout = FinancialBuyerCandidateRetriever([StaticRetriever("PEDealActivityRetriever", [hit])])

    result = fanout.retrieve(sample_profile())

    assert result.hits == [hit]
    assert result.metadata["hit_count"] == 1
    assert result.metadata["retrievers"][0]["name"] == "PEDealActivityRetriever"


def test_financial_candidates_api_returns_target_profile_and_hits(tmp_path: Path, monkeypatch) -> None:
    hit = financial_hit()
    monkeypatch.setattr("src.api.main.build_target_profile_extractor", lambda _settings, _session: FakeProfileService())
    monkeypatch.setattr(
        "src.api.main.build_financial_buyer_candidate_retriever",
        lambda _settings, _session: StaticRetriever("FinancialBuyerCandidateRetriever", [hit]),
    )
    app = create_app(settings_for_tests(tmp_path))

    with TestClient(app) as client:
        response = client.get("/buyers/financial-candidates?query=ELF")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_profile"]["ticker"] == "ELF"
    assert payload["hits"][0]["candidate_name"] == "Bain Capital"
    assert payload["hits"][0]["buyer_type"] == "financial"
    assert payload["metadata"]["retrieval"]["retriever"] == "FinancialBuyerCandidateRetriever"


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


def seed_universe() -> PESeedUniverse:
    return PESeedUniverse(
        firms=[
            PESeedFirm(canonical_name="Bain Capital", aliases=["Bain Capital Private Equity"], domain="baincapital.com"),
            PESeedFirm(canonical_name="KKR", aliases=["Kohlberg Kravis Roberts"], domain="kkr.com"),
        ]
    )


def fmp_deal(
    transaction_id: str,
    acquirer: str,
    target: str,
    transaction_date: str,
    *,
    raw_text: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        source_id="fmp",
        source_dimension="buyer_long_list_recall.financial_sponsor_activity",
        source_type=SourceType.transaction_signal,
        source_strength=SourceStrength.B,
        url=f"https://www.sec.gov/Archives/{transaction_id}",
        raw_text=raw_text or f"{acquirer} acquired {target}.",
        metadata={
            "transactionId": transaction_id,
            "acquiringCompanyName": acquirer,
            "acquiredCompanyName": target,
            "transactionDate": transaction_date,
            "filingUrl": f"https://www.sec.gov/Archives/{transaction_id}",
            "record_role": "mergers_acquisitions_result",
        },
        retrieved_at=datetime(2026, 5, 4, tzinfo=UTC),
    )


def financial_hit() -> CandidateHit:
    return CandidateHit(
        candidate_name="Bain Capital",
        candidate_domain="baincapital.com",
        buyer_type=BuyerType.financial,
        retriever_name="PEDealActivityRetriever",
        source_path=["pe_deal_activity"],
        fit_reason="Completed one same-sector PE deal signal.",
        evidence=[
            Evidence(
                claim="Bain Capital had a transaction signal involving Cosmetics Labs.",
                source_type="transaction_signal",
                source_dimension="buyer_long_list_recall.financial_sponsor_activity",
                source_strength=SourceStrength.B,
                url="https://www.sec.gov/Archives/deal-1",
                verified_fact=True,
            )
        ],
        confidence=0.76,
    )


class FakeFmpDealSource:
    def __init__(self, pages_by_query: dict[str, list[list[SourceDocument]]]) -> None:
        self.pages_by_query = pages_by_query
        self.calls: list[dict[str, Any]] = []

    def search_mergers_acquisitions(self, name: str, ttl_hours: int, **kwargs: Any) -> list[SourceDocument]:
        self.calls.append({"name": name, "ttl_hours": ttl_hours, **kwargs})
        page = int(kwargs.get("page", 0))
        pages = self.pages_by_query.get(name, [])
        if page >= len(pages):
            return []
        return [
            document.model_copy(
                update={
                    "source_strength": kwargs.get("source_strength", document.source_strength),
                    "source_dimension": kwargs.get("source_dimension", document.source_dimension),
                }
            )
            for document in pages[page]
        ]


class StaticRetriever:
    def __init__(self, name: str, hits: list[CandidateHit]) -> None:
        self.name = name
        self.hits = hits

    def retrieve(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})


class FakeProfileService:
    def build_profile(self, _query: str):
        class ProfileResult:
            target_profile = sample_profile()
            warnings: list[str] = []
            extraction_metadata = {"extractor_version": "test"}

        return ProfileResult()
