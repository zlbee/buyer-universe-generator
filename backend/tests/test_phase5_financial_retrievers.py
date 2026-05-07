from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.config import Settings
from src.domain import BuyerType, CandidateHit, Evidence, SourceDocument, SourceStrength, SourceType, StrategicRetrievalResult, TargetProfile
from src.repositories.buyer_recall_cache import BuyerRecallCache
from src.repositories.database import create_session_factory, init_db
from src.retrievers import FinancialBuyerCandidateRetriever, PEDealActivityRetriever
from src.retrievers.financial.seed_universe import PESeedFirm, PESeedUniverse
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "retrieval_rules_path": Path("config/retrieval_rules.yaml"),
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

    sponsor_source = strategy.selected_source("financial_sponsor_activity", "fmp", include_disabled=True)
    llm_sponsor_source = strategy.selected_source("financial_sponsor_activity", "llm_web_search", include_disabled=True)
    policy = strategy.retriever_config("PEDealActivityRetriever")

    assert sponsor_source is not None
    assert llm_sponsor_source is not None
    assert sponsor_source.dimension_id == "potential_buyer_discovery.financial_sponsor_activity"
    assert sponsor_source.source_strength == SourceStrength.B
    assert llm_sponsor_source.source_strength == SourceStrength.C
    assert llm_sponsor_source.config.retrieval.web_search_max_results == 10
    assert llm_sponsor_source.config.retrieval.web_search_max_total_results == 40
    assert llm_sponsor_source.config.retrieval.web_fetch_max_uses == 5
    assert llm_sponsor_source.config.retrieval.web_fetch_max_content_tokens == 12000
    assert policy is not None
    assert policy.use_case == "financial_sponsor_activity"
    assert strategy.source_role_map(policy.use_case, include_disabled=True) == {
        "deal_activity_source": "fmp",
        "llm_web_search_source": "llm_web_search",
    }
    assert [source.source_id for source in strategy.selected_sources_by_priority(policy.use_case, include_disabled=True)] == [
        "fmp",
        "llm_web_search",
    ]
    assert policy.lookback_years == 5
    assert policy.max_companies == 30
    assert policy.web_search_batch_size == 10
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
    assert bain.data_source == ["fmp"]
    assert bain.confidence == 0.76
    assert bain.evidence[0].source_dimension == "potential_buyer_discovery.financial_sponsor_activity"
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
    assert fmp_source.calls[1]["source_dimension"] == "potential_buyer_discovery.financial_sponsor_activity"
    assert any("older than 2021-05-04" in warning for warning in result.warnings)
    assert any("non-seed acquirer" in warning for warning in result.warnings)
    assert any("unrelated FMP deal" in warning for warning in result.warnings)


def test_pe_deal_activity_retriever_adds_llm_web_search_deal_signals(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path, fmp_api_key=None))
    web_search = FakeWebSearchJSONClient(
        {
            "pe_firm": "Advent International",
            "sic": "2844",
            "acquisitions": [
                {
                    "has_relevant_deal": True,
                    "evidence": "Advent International announced the acquisition of Beauty Labs, a cosmetics company.",
                    "deal_date": "March 2025",
                    "target": "Beauty Labs",
                    "sector_relevance": "Same-industry: cosmetics / color cosmetics / prestige beauty products",
                    "source_links": [
                        {
                            "url": "https://www.adventinternational.com/beauty-labs",
                            "title": "Advent International acquires Beauty Labs",
                        }
                    ],
                    "type": "acquisition",
                }
            ],
        }
    )
    retriever = PEDealActivityRetriever(
        strategy,
        fmp_client=FakeFmpDealSource({}),
        web_search_client=web_search,
        seed_universe=PESeedUniverse(
            firms=[
                PESeedFirm(canonical_name="Advent International", aliases=["Advent"], domain="adventinternational.com"),
            ]
        ),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Advent International"]
    hit = result.hits[0]
    assert hit.buyer_type == BuyerType.financial
    assert hit.source_path == ["pe_deal_activity_llm_web_search"]
    assert hit.data_source == ["llm_web_search"]
    assert hit.pending_verification is True
    assert hit.confidence == 0.58
    assert hit.evidence[0].verified_fact is False
    assert hit.evidence[0].url == "https://www.adventinternational.com/beauty-labs"
    assert hit.evidence[0].quote_or_snippet == "Advent International announced the acquisition of Beauty Labs, a cosmetics company."
    assert hit.retrieval_metadata["deal_events"][0]["deal_date"] == "2025-03-01"
    assert hit.retrieval_metadata["deal_events"][0]["target_acquired"] == "Beauty Labs"
    assert hit.retrieval_metadata["deal_events"][0]["sector_match"] == "same"
    assert hit.retrieval_metadata["llm_web_search_event_count"] == 1
    assert result.metadata["llm_web_search_documents_checked"] == 1
    assert result.metadata["llm_web_search_deal_count"] == 1
    assert web_search.source_business_types == ["financial_buyer_pe_deal_activity_web_search"]
    assert web_search.calls[0]["max_results"] == 10
    assert web_search.calls[0]["max_total_results"] == 40
    assert web_search.calls[0]["fetch_max_uses"] == 5
    assert web_search.calls[0]["fetch_max_content_tokens"] == 12000
    assert web_search.calls[0]["json_schema"]["properties"]["firms"]["maxItems"] == 1
    assert "Use web search" in web_search.prompts[0]
    assert "Advent International" in web_search.prompts[0]
    assert "SIC=2844" in web_search.prompts[0]
    assert "2021-05-04" in web_search.prompts[0]
    assert "e.l.f. Beauty, Inc." not in web_search.prompts[0]
    assert "Adjacent-industry terms" in web_search.prompts[0]


def test_pe_deal_activity_retriever_batches_seed_firms_for_llm_by_default(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path, fmp_api_key=None))
    web_search = FakeWebSearchJSONClient({"pe_firm": None, "sic": "2844", "deals": []})
    retriever = PEDealActivityRetriever(
        strategy,
        web_search_client=web_search,
        seed_universe=PESeedUniverse(
            firms=[
                PESeedFirm(canonical_name="Advent International"),
                PESeedFirm(canonical_name="Bain Capital"),
                PESeedFirm(canonical_name="KKR"),
            ]
        ),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert result.hits == []
    assert len(web_search.prompts) == 1
    assert "Advent International" in web_search.prompts[0]
    assert "Bain Capital" in web_search.prompts[0]
    assert "KKR" in web_search.prompts[0]
    assert web_search.calls[0]["json_schema"]["properties"]["firms"]["maxItems"] == 3
    assert result.metadata["seed_firm_count"] == 3
    assert result.metadata["seed_firms_checked"] == 3
    assert result.metadata["llm_web_search_batch_size"] == 10
    assert result.metadata["llm_web_search_batches_checked"] == 1
    assert result.metadata["llm_web_search_no_deal_firm_count"] == 3


def test_pe_deal_activity_retriever_parses_batched_llm_firm_results(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path, fmp_api_key=None))
    web_search = FakeWebSearchJSONClient(
        {
            "sic": "2844",
            "firms": [
                {
                    "pe_firm": "Advent International",
                    "deals": [
                        {
                            "has_relevant_deal": True,
                            "evidence_summary": "Advent International acquired Beauty Labs, a cosmetics company.",
                            "acquisition_date": "2025-03-01",
                            "acquired_company": "Beauty Labs",
                            "sector_relevance": "same",
                            "source_url": "https://www.adventinternational.com/beauty-labs",
                            "source_title": "Advent International acquires Beauty Labs",
                            "deal_type": "acquisition",
                        }
                    ],
                },
                {"pe_firm": "Bain Capital", "deals": []},
            ],
        }
    )
    retriever = PEDealActivityRetriever(
        strategy,
        web_search_client=web_search,
        seed_universe=PESeedUniverse(
            firms=[
                PESeedFirm(canonical_name="Advent International"),
                PESeedFirm(canonical_name="Bain Capital"),
            ]
        ),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Advent International"]
    assert len(web_search.prompts) == 1
    assert result.metadata["llm_web_search_documents_checked"] == 1
    assert result.metadata["llm_web_search_no_deal_firm_count"] == 1


def test_pe_deal_activity_retriever_preserves_llm_hits_under_document_cap(tmp_path: Path) -> None:
    strategy = DataSourceStrategy.from_settings(settings_for_tests(tmp_path))
    web_search = FakeWebSearchJSONClient(
        {
            "pe_firm": "Advent International",
            "sic": "2844",
            "deals": [
                {
                    "evidence": "Advent International announced the acquisition of Beauty Labs, a cosmetics company.",
                    "deal_date": "2025",
                    "target": "Beauty Labs",
                    "url": "https://www.adventinternational.com/beauty-labs",
                    "type": "acquisition",
                }
            ],
        }
    )
    fmp_source = FakeFmpDealSource(
        {
            "Perfumes, cosmetics, and other toilet preparations": [
                [fmp_deal(f"fmp-{index}", "Strategic Corp", f"Cosmetics Labs {index}", "2025-06-01") for index in range(100)]
            ]
        }
    )
    retriever = PEDealActivityRetriever(
        strategy,
        fmp_client=fmp_source,
        web_search_client=web_search,
        seed_universe=PESeedUniverse(firms=[PESeedFirm(canonical_name="Advent International")]),
        as_of_date=date(2026, 5, 4),
    )

    result = retriever.retrieve_with_context(sample_profile())

    assert [hit.candidate_name for hit in result.hits] == ["Advent International"]
    assert result.hits[0].source_path == ["pe_deal_activity_llm_web_search"]
    assert result.metadata["documents_available_before_cap"] == 101
    assert result.metadata["documents_truncated"] == 1
    assert result.metadata["documents_checked"] == 100


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


def test_financial_buyer_candidate_retriever_caches_stage_result_for_same_seller(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    retriever = CountingRetriever("PEDealActivityRetriever", [financial_hit()])

    with session_factory() as session:
        fanout = FinancialBuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache",
        )
        first = fanout.retrieve(sample_profile())

    with session_factory() as session:
        fanout = FinancialBuyerCandidateRetriever(
            [retriever],
            cache=BuyerRecallCache(session),
            cache_ttl_hours=24,
            stage_version="test-buyer-recall-cache",
        )
        second = fanout.retrieve(sample_profile())

    assert retriever.calls == 1
    assert [hit.candidate_name for hit in second.hits] == ["Bain Capital"]
    assert first.metadata["cache"]["status"] == "miss"
    assert second.metadata["cache"]["status"] == "hit"
    assert second.metadata["cache"]["stage_name"] == "financial_buyer_recall"


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
    assert payload["hits"][0]["data_source"] == ["fmp"]
    assert payload["metadata"]["retrieval"]["retriever"] == "FinancialBuyerCandidateRetriever"


def test_combined_candidates_api_runs_strategic_and_financial_retrievers(tmp_path: Path, monkeypatch) -> None:
    strategic_hit = CandidateHit(
        candidate_name="Beauty Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="SameSicRetriever",
        source_path=["same_sic"],
        data_source=["edgar"],
        fit_reason="Shares target SIC.",
        evidence=[
            Evidence(
                claim="Beauty Buyer shares a public-company industry signal.",
                source_type="sec_company_mapping",
                source_strength=SourceStrength.B,
                url="https://www.sec.gov/files/company_tickers_exchange.json",
            )
        ],
        confidence=0.72,
    )
    financial = financial_hit()
    monkeypatch.setattr("src.api.main.build_target_profile_extractor", lambda _settings, _session: FakeProfileService())
    monkeypatch.setattr(
        "src.api.main.build_strategic_buyer_candidate_retriever",
        lambda _settings, _session: StaticRetriever("StrategicBuyerCandidateRetriever", [strategic_hit]),
    )
    monkeypatch.setattr(
        "src.api.main.build_financial_buyer_candidate_retriever",
        lambda _settings, _session: StaticRetriever("FinancialBuyerCandidateRetriever", [financial]),
    )
    app = create_app(settings_for_tests(tmp_path))

    with TestClient(app) as client:
        response = client.get("/buyers/candidates?query=ELF")

    assert response.status_code == 200
    payload = response.json()
    assert [hit["candidate_name"] for hit in payload["hits"]] == ["Beauty Buyer Inc.", "Bain Capital"]
    assert [hit["buyer_type"] for hit in payload["hits"]] == ["strategic", "financial"]
    assert [hit["data_source"] for hit in payload["hits"]] == [["edgar"], ["fmp"]]
    assert payload["metadata"]["retrieval"]["hit_count"] == 2
    assert payload["metadata"]["retrieval"]["strategic"]["retriever"] == "StrategicBuyerCandidateRetriever"
    assert payload["metadata"]["retrieval"]["financial"]["retriever"] == "FinancialBuyerCandidateRetriever"


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
        source_dimension="potential_buyer_discovery.financial_sponsor_activity",
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
        data_source=["fmp"],
        fit_reason="Completed one same-sector PE deal signal.",
        evidence=[
            Evidence(
                claim="Bain Capital had a transaction signal involving Cosmetics Labs.",
                source_type="transaction_signal",
                source_dimension="potential_buyer_discovery.financial_sponsor_activity",
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


class FakeWebSearchJSONClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
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
        return self.payload


class StaticRetriever:
    def __init__(self, name: str, hits: list[CandidateHit]) -> None:
        self.name = name
        self.hits = hits

    def retrieve(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})


class CountingRetriever(StaticRetriever):
    def __init__(self, name: str, hits: list[CandidateHit]) -> None:
        super().__init__(name, hits)
        self.calls = 0

    def retrieve(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        self.calls += 1
        return StrategicRetrievalResult(hits=self.hits, metadata={"retriever": self.name})


class FakeProfileService:
    def build_profile(self, _query: str):
        class ProfileResult:
            target_profile = sample_profile()
            warnings: list[str] = []
            extraction_metadata = {"extractor_version": "test"}

        return ProfileResult()
