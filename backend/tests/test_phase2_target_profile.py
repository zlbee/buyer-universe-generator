from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.main import create_app
from src.cli import main as cli_main
from src.config import Settings
from src.domain import (
    FilingMetadata,
    ResolvedTarget,
    SourceDocument,
    SourceStrength,
    SourceType,
    TargetIngestionResult,
    TargetProfile,
    TargetProfileExtractionResult,
)
from src.llm import LLMResponseError, OpenRouterProvider
from src.pipelines.source_ingestion import SourceIngestionService
from src.pipelines.target_profile_extraction import TargetProfileExtractionError, TargetProfileExtractor
from src.repositories.data_source_audit_log import DataSourceAuditLog
from src.repositories.database import create_session_factory, init_db
from src.repositories.llm_interaction_log import LLMInteractionLog
from src.repositories.models import DataSourceRawRecordRecord, DataSourceRequestRecord, LLMInteractionRecord
from src.repositories.source_cache import SourceCache
from src.repositories.target_profile_cache import TargetProfileCache
from src.sources.company_pages import CompanyPageClient
from src.sources.investor_relations import IRPageDiscoveryResult, InvestorRelationsPageDiscovery
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
        "openrouter_api_key": "openrouter-test-key",
        "polygon_api_key": None,
        "news_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_sec_filing_text_document_requires_edgartools_structured_object(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(text="ITEM 1. Business fallback text.")
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    with pytest.raises(ValueError, match="Could not retrieve structured filing section"):
        client.fetch_filing_text_document(
            sample_target(),
            sample_filing("10-K"),
            ttl_hours=24,
            source_strength=SourceStrength.B,
            source_dimension="seller_profile.business_description",
        )

    assert fake_edgar.filing_calls == ["0000923796-25-000001"]


def test_sec_filing_text_document_prefers_edgartools_structured_object(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(
        text="ITEM 1. Business fallback text.",
        report_object=FakeTenKReport(
            business="Structured Item 1. Business e.l.f. Beauty sells cosmetics.",
            management_discussion="Structured Item 7. Management discusses digital commerce.",
        ),
    )
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    document = client.fetch_filing_text_document(
        sample_target(),
        sample_filing("10-K"),
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="seller_profile.business_description",
    )

    assert document.raw_text == "Structured Item 1. Business e.l.f. Beauty sells cosmetics."
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.obj"
    assert document.metadata["text_scope"] == "item_1_business"
    assert document.metadata["structured_sections"]["management_discussion"] == "Structured Item 7. Management discusses digital commerce."
    assert fake_edgar.obj_calls == 1


def test_sec_filing_text_document_errors_when_structured_section_missing(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(markdown="# 10-K\n\nITEM 1. Business markdown audit text.", report_object=FakeTenKReport())
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        client = SecEdgarClient(settings, edgar_module=fake_edgar, request_recorder=DataSourceAuditLog(session))
        with pytest.raises(ValueError, match="Could not retrieve structured filing section"):
            client.fetch_filing_text_document(sample_target(), sample_filing("10-K"), ttl_hours=24)

        request_record = session.execute(select(DataSourceRequestRecord)).scalar_one()
        raw_record = session.execute(select(DataSourceRawRecordRecord)).scalar_one()

    assert request_record.operation == "edgartools_filing_structured_object"
    assert request_record.status == "error"
    assert "did not return the required structured section" in (request_record.error_message or "")
    assert raw_record.raw_text == "# 10-K\n\nITEM 1. Business markdown audit text."
    raw_metadata = json.loads(raw_record.metadata_json)
    assert raw_metadata["record_role"] == "filing_structured_object"
    assert raw_metadata["raw_text_representation"] == "edgartools:Filing.markdown:audit"


def test_sec_filing_text_document_uses_configured_markdown_item_parser(tmp_path: Path) -> None:
    business_sentence = (
        "e.l.f. Beauty sells cosmetics through digital commerce, retail channels, international stores, "
        "and brand-led innovation across color cosmetics and skin care. "
    )
    business_body = business_sentence * 6
    markdown = (
        "# Form 10-K\n\n"
        "Item 1. Business 5\n"
        "Item 1A. Risk Factors 18\n\n"
        f"Item 1. Business\n\n{business_body}\n\n"
        "Item 1A. Risk Factors\n\nRisk text."
    )
    fake_edgar = FakeEdgarTextModule(markdown=markdown, report_object=FakeTenKReport())
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        client = SecEdgarClient(
            settings,
            edgar_module=fake_edgar,
            request_recorder=DataSourceAuditLog(session),
            markdown_item_parser_enabled=True,
        )
        document = client.fetch_filing_text_document(
            sample_target(),
            sample_filing("10-K"),
            ttl_hours=24,
            source_dimension="seller_profile.business_description",
        )

        request_record = session.execute(select(DataSourceRequestRecord)).scalar_one()
        raw_record = session.execute(select(DataSourceRawRecordRecord)).scalar_one()

    assert document.raw_text.startswith("Item 1. Business")
    assert "Risk Factors" not in document.raw_text
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.markdown:item_parser"
    assert document.metadata["text_scope"] == "item_1_business"
    assert document.metadata["structured_sections"]["business"] == document.raw_text
    assert request_record.status == "success"
    assert raw_record.raw_text == markdown
    raw_payload = json.loads(raw_record.raw_payload_json)
    raw_metadata = json.loads(raw_record.metadata_json)
    assert raw_payload["markdown_item_parser"]["result"] == "matched"
    assert raw_metadata["text_retrieval_method"] == "edgartools:Filing.markdown:item_parser"


def test_sec_filing_text_document_extracts_strategy_mdna_scope(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(
        report_object=FakeTenKReport(
            business="Structured Item 1. Business e.l.f. Beauty sells cosmetics.",
            management_discussion=(
                "Structured Item 7. Management's Discussion and Analysis We intend to invest in digital commerce "
                "and pursue strategic acquisitions."
            ),
        )
    )
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    document = client.fetch_filing_text_document(
        sample_target(),
        sample_filing("10-K"),
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="seller_profile.company_strategy",
        text_scope="company_strategy",
    )

    assert document.raw_text.startswith("Structured Item 7")
    assert "invest in digital commerce and pursue strategic acquisitions" in document.raw_text
    assert document.source_dimension == "seller_profile.company_strategy"
    assert document.metadata["text_scope"] == "item_7_mdna"
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.obj"


def test_sec_filing_text_document_uses_markdown_item_parser_for_strategy(tmp_path: Path) -> None:
    mdna_sentence = (
        "Management discusses digital commerce, brand investment, international expansion, supply chain scale, "
        "and selective acquisition opportunities as part of the company's strategy. "
    )
    mdna_body = mdna_sentence * 6
    markdown = (
        "# Form 10-K\n\n"
        "Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations\n\n"
        f"{mdna_body}\n\n"
        "Item 7A. Quantitative and Qualitative Disclosures About Market Risk\n\nMarket risk text."
    )
    fake_edgar = FakeEdgarTextModule(markdown=markdown, report_object=FakeTenKReport())
    client = SecEdgarClient(
        settings_for_tests(tmp_path),
        edgar_module=fake_edgar,
        markdown_item_parser_enabled=True,
    )

    document = client.fetch_filing_text_document(
        sample_target(),
        sample_filing("10-K"),
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="seller_profile.company_strategy",
        text_scope="company_strategy",
    )

    assert document.raw_text.startswith("Item 7. Management's Discussion")
    assert "Market Risk" not in document.raw_text
    assert document.metadata["text_scope"] == "item_7_mdna"
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.markdown:item_parser"


def test_source_ingestion_restores_filing_text_metadata() -> None:
    filing = sample_filing("10-K")
    document = SourceDocument(
        source_id="edgar",
        source_dimension="seller_profile.business_description",
        source_type=SourceType.sec_filing,
        source_strength=SourceStrength.B,
        target_cik=sample_target().cik,
        target_ticker=sample_target().ticker,
        url=filing.url,
        filing_accession=filing.accession_number,
        raw_text="ITEM 1. Business e.l.f. Beauty sells cosmetics.",
        metadata={
            **filing.model_dump(mode="json"),
            "text_retrieval_method": "edgartools:Filing.text",
            "text_scope": "item_1_business",
            "raw_text_char_count": 394227,
            "cached_text_char_count": 18,
            "structured_sections": {
                "business": "ITEM 1. Business e.l.f. Beauty sells cosmetics.",
                "management_discussion": "ITEM 7. Management discusses channel expansion.",
            },
            "structured_section_char_counts": {
                "business": 47,
                "management_discussion": 47,
            },
        },
    )
    service = object.__new__(SourceIngestionService)

    restored_filings = service._filings_from_documents([document])

    assert restored_filings[0].accession_number == filing.accession_number
    assert restored_filings[0].text_retrieval_method == "edgartools:Filing.text"
    assert restored_filings[0].text_scope == "item_1_business"
    assert restored_filings[0].raw_text_char_count == 394227
    assert restored_filings[0].cached_text_char_count == 18
    assert restored_filings[0].structured_sections["business"] == "ITEM 1. Business e.l.f. Beauty sells cosmetics."
    assert restored_filings[0].structured_section_char_counts["management_discussion"] == 47


def test_company_page_client_prefers_investor_page_over_homepage(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        if url == "https://www.elfcosmetics.com":
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    "Afterpay: Shop Now, Pay in 4 interest-free payments!"
                    '<a href="https://investor.elfbeauty.com">Investors</a>'
                    "</body></html>"
                ),
            )
        if url == "https://investor.elfbeauty.com":
            return httpx.Response(200, text="<html><body>e.l.f. Beauty is a multi-brand beauty company.</body></html>")
        return httpx.Response(404)

    client = CompanyPageClient(settings_for_tests(tmp_path), http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    documents = client.fetch_official_pages(
        sample_target(),
        "https://www.elfcosmetics.com",
        ttl_hours=24,
        source_strength=SourceStrength.A,
        source_dimension="seller_profile.business_description",
    )

    assert [document.url for document in documents] == ["https://investor.elfbeauty.com"]
    assert documents[0].metadata["page_role"] == "investor_relations"
    assert "multi-brand beauty company" in documents[0].raw_text


def test_openrouter_provider_sends_structured_chat_request(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"business_summary": "A beauty company."})}}]},
        )

    settings = settings_for_tests(tmp_path, openrouter_base_url="https://openrouter.test/api/v1")
    provider = OpenRouterProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    json_schema = {"type": "object", "properties": {"business_summary": {"type": "string"}}}

    result = provider.generate_json("extract profile", "TargetProfileFeatureExtraction", json_schema)

    assert result["business_summary"] == "A beauty company."
    assert captured["headers"]["Authorization"] == "Bearer openrouter-test-key"
    assert captured["payload"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "TargetProfileFeatureExtraction",
            "strict": True,
            "schema": json_schema,
        },
    }
    assert captured["payload"]["provider"] == {"require_parameters": True}
    assert "temperature" not in captured["payload"]
    assert "target company profile" not in captured["payload"]["messages"][0]["content"]
    assert captured["payload"]["messages"][1]["content"] == "extract profile"


def test_openrouter_provider_sends_web_search_server_tool_request(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"candidates": []})}}]},
        )

    settings = settings_for_tests(tmp_path, openrouter_base_url="https://openrouter.test/api/v1")
    provider = OpenRouterProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.generate_json_with_web_search(
        "find investor relations page",
        "InvestorRelationsPageDiscovery",
        {"type": "object"},
        max_results=3,
        max_total_results=3,
        search_engine="exa",
        fetch_max_uses=20,
        fetch_max_content_tokens=50000,
    )

    assert result == {"candidates": []}
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa",
                "max_results": 3,
                "max_total_results": 3,
                "search_context_size": "low",
            },
        },
        {
            "type": "openrouter:web_fetch",
            "parameters": {
                "engine": "auto",
                "max_uses": 20,
                "max_content_tokens": 50000,
            },
        },
    ]
    assert "provider" not in captured["payload"]


def test_openrouter_provider_persists_raw_llm_interaction(tmp_path: Path) -> None:
    raw_content = json.dumps({"business_summary": "A beauty company."})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": raw_content}}]})

    settings = settings_for_tests(tmp_path, openrouter_base_url="https://openrouter.test/api/v1")
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        provider = OpenRouterProvider(
            settings,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            interaction_recorder=LLMInteractionLog(session),
        )

        result = provider.generate_json(
            "extract profile",
            "TargetProfileFeatureExtraction",
            {"type": "object"},
            system_prompt="Return target JSON.",
            source_business_type="target_profile_extraction",
        )

        record = session.execute(select(LLMInteractionRecord)).scalar_one()

    assert result == {"business_summary": "A beauty company."}
    assert record.source_business_type == "target_profile_extraction"
    assert record.provider == "openrouter"
    assert record.model == settings.llm_model
    assert record.schema_name == "TargetProfileFeatureExtraction"
    assert record.status == "success"
    assert record.prompt == "extract profile"
    assert record.system_prompt == "Return target JSON."
    assert record.raw_output_content == raw_content
    assert json.loads(record.parsed_output_json or "{}") == result
    assert json.loads(record.request_payload_json)["messages"][1]["content"] == "extract profile"


def test_ir_page_discovery_selects_elf_investor_page_and_rejects_storefront(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        if url == "https://www.elfcosmetics.com":
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>e.l.f. Cosmetics</title></head><body>"
                    "Afterpay: Shop Now, Pay in 4 interest-free payments!"
                    "<a href='https://investor.elfbeauty.com'>Investors</a>"
                    "</body></html>"
                ),
            )
        if url == "https://investor.elfbeauty.com":
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>e.l.f. Beauty - Investor Relations</title></head>"
                    "<body>Investor Relations SEC Filings Financials e.l.f. Beauty is a multi-brand beauty company.</body></html>"
                ),
            )
        return httpx.Response(404)

    fake_web_search = FakeWebSearchJSONClient(
        {
            "candidates": [
                {
                    "url": "https://www.elfcosmetics.com",
                    "title": "e.l.f. Cosmetics",
                    "snippet": "Official beauty shopping site.",
                    "confidence": 0.55,
                    "reason": "Known company website",
                    "source": "web",
                },
                {
                    "url": "https://investor.elfbeauty.com/",
                    "title": "e.l.f. Beauty - Investor Relations",
                    "snippet": "Investor Relations, SEC filings, financials and stock information.",
                    "confidence": 0.95,
                    "reason": "Official IR page",
                    "source": "web",
                },
            ]
        }
    )
    discovery = InvestorRelationsPageDiscovery(
        settings_for_tests(tmp_path),
        fake_web_search,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )

    result = discovery.discover(
        sample_target(),
        "https://www.elfcosmetics.com",
        ttl_hours=24,
        source_strength=SourceStrength.A,
        source_dimension="seller_profile.business_description",
    )

    assert result.status == "selected"
    assert result.document is not None
    assert result.document.url == "https://investor.elfbeauty.com/"
    assert result.document.metadata["page_role"] == "investor_relations"
    assert result.document.metadata["discovered_from"] == "llm_web_search"
    assert result.document.metadata["candidate_rank"] == 2
    assert "investor relations" in result.document.metadata["validated_signals"]
    assert result.rejection_reasons
    assert "missing investor-relations page signals" in result.rejection_reasons[0]
    assert fake_web_search.source_business_types == ["investor_relations_page_discovery"]


def test_openrouter_provider_recovers_wrapped_json_content(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "```json\n{\"business_summary\":\"A beauty company.\"}\n```",
                        }
                    }
                ]
            },
        )

    settings = settings_for_tests(tmp_path, openrouter_base_url="https://openrouter.test/api/v1")
    provider = OpenRouterProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.generate_json("extract profile", "TargetProfileFeatureExtraction", {"type": "object"})

    assert result == {"business_summary": "A beauty company."}


def test_openrouter_provider_unwraps_schema_named_object(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"TargetProfileFeatureExtraction": {"business_summary": "A beauty company."}}
                            ),
                        }
                    }
                ]
            },
        )

    settings = settings_for_tests(tmp_path, openrouter_base_url="https://openrouter.test/api/v1")
    provider = OpenRouterProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.generate_json("extract profile", "TargetProfileFeatureExtraction", {"type": "object"})

    assert result == {"business_summary": "A beauty company."}


def test_target_profile_extractor_assembles_profile_and_reuses_cache(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "business_summary": "e.l.f. Beauty sells cosmetics through retail and e-commerce.",
            "company_strategy": "e.l.f. Beauty intends to invest in digital commerce and pursue strategic acquisitions.",
            "products": ["cosmetics"],
            "customer_segments": ["retail consumers"],
            "channels": ["retail", "e-commerce"],
            "geographies": ["United States"],
            "keywords": ["cosmetics", "beauty", "unsupported trend"],
            "adjacent_categories": ["personal care"],
            "field_support": {
                "business_summary": ["sells cosmetics through retail and e-commerce"],
                "company_strategy": ["invest in digital commerce and pursue strategic acquisitions"],
                "products": ["sells cosmetics"],
                "customer_segments": ["retail consumers"],
                "channels": ["retail and e-commerce"],
                "geographies": ["United States"],
            },
        }
    )

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, fake_llm)
        first_result = extractor.build_profile("ELF")
        second_result = extractor.build_profile("ELF")

    profile = first_result.target_profile
    assert profile.ticker == "ELF"
    assert profile.company_strategy == "e.l.f. Beauty intends to invest in digital commerce and pursue strategic acquisitions."
    assert profile.products == ["cosmetics"]
    assert profile.keywords == [
        "cosmetics",
        "retail consumers",
        "retail",
        "e-commerce",
        "beauty products",
        "personal care",
        "toiletries",
        "beauty",
    ]
    assert profile.keyword_groups == {
        "verified_products": ["cosmetics"],
        "verified_customer_segments": ["retail consumers"],
        "verified_channels": ["retail", "e-commerce"],
        "sic_taxonomy": ["beauty products", "personal care", "toiletries"],
        "source_matched_llm_keyword": ["beauty"],
    }
    assert "unsupported trend" not in profile.keywords
    assert profile.feature_labels["business_summary"] == "verified_fact"
    assert profile.feature_labels["company_strategy"] == "verified_fact"
    assert profile.feature_labels["keywords"] == "derived_keyword"
    assert profile.feature_labels["adjacent_categories"] == "llm_inference"
    assert profile.feature_evidence["business_summary"][0].source_dimension == "seller_profile.business_description"
    assert profile.feature_evidence["company_strategy"][0].source_dimension == "seller_profile.company_strategy"
    assert profile.feature_evidence["keywords"][0].claim == "Keyword 'cosmetics' is derived from verified products."
    assert first_result.extraction_metadata["keyword_derivation_counts"] == {
        "verified_products": 1,
        "verified_customer_segments": 1,
        "verified_channels": 2,
        "sic_taxonomy": 3,
        "source_matched_llm_keyword": 1,
    }
    assert first_result.extraction_metadata["keyword_taxonomy_version"] == 1
    assert first_result.extraction_metadata["cache_hit"] is False
    assert second_result.extraction_metadata["cache_hit"] is True
    assert fake_llm.calls == 1
    assert fake_llm.schema_names == ["TargetProfileFeatureExtraction"]
    assert fake_llm.source_business_types == ["target_profile_extraction"]
    assert fake_llm.system_prompts[0].startswith("You are extracting a target company profile")
    assert fake_llm.json_schemas[0]["additionalProperties"] is False
    assert "company_strategy" in fake_llm.json_schemas[0]["required"]
    assert "field_support" in fake_llm.json_schemas[0]["required"]
    assert fake_llm.json_schemas[0]["properties"]["business_summary"]["anyOf"][0]["type"] == "string"
    assert fake_llm.json_schemas[0]["properties"]["company_strategy"]["anyOf"][0]["type"] == "string"
    assert fake_llm.json_schemas[0]["properties"]["field_support"]["additionalProperties"] is False
    assert "source_dimension=seller_profile.company_strategy" in fake_llm.prompts[0]


def test_target_profile_extractor_reuses_structured_sections_for_strategy(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        extractor = build_test_extractor(
            settings,
            session,
            FakeLLMClient({}),
            edgar_client=FailingProfileSecClient(),
        )
        ingestion = FakeStructuredSectionsIngestionService().ingest("ELF")
        warnings: list[str] = []

        documents = extractor._ensure_strategy_filing_text(ingestion, list(ingestion.source_documents), warnings)

    strategy_document = next(document for document in documents if document.source_dimension == "seller_profile.company_strategy")
    assert warnings == []
    assert "invest in digital commerce and pursue strategic acquisitions" in strategy_document.raw_text
    assert strategy_document.metadata["text_retrieval_method"] == "cache:structured_sections"
    assert strategy_document.metadata["derived_from_source_dimension"] == "seller_profile.business_description"


def test_target_profile_extractor_normalizes_repairable_llm_shape(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "business_summary": "e.l.f. Beauty sells cosmetics through retail and e-commerce.",
            "products": "cosmetics",
            "customer_segments": ["retail consumers"],
            "channels": "retail",
            "geographies": ["United States"],
            "keywords": "beauty",
            "adjacent_categories": "personal care",
            "field_support": {
                "business_summary": "sells cosmetics through retail and e-commerce",
                "products": "sells cosmetics",
                "channels": "retail and e-commerce",
            },
        }
    )

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, fake_llm)
        result = extractor.build_profile("ELF")

    assert result.target_profile.products == ["cosmetics"]
    assert result.target_profile.channels == ["retail"]
    assert result.target_profile.keywords == [
        "cosmetics",
        "retail",
        "beauty products",
        "personal care",
        "toiletries",
        "beauty",
    ]
    assert result.target_profile.feature_labels["business_summary"] == "verified_fact"


def test_target_profile_extractor_rejects_low_value_company_page_support(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "channels": ["e-commerce"],
            "field_support": {
                "channels": ["Afterpay: Shop Now, Pay in 4 interest-free payments!"],
            },
        }
    )

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, fake_llm, ingestion_service=FakeNoisyCompanyPageIngestionService())
        result = extractor.build_profile("ELF")

    assert result.target_profile.channels == ["e-commerce"]
    assert result.target_profile.feature_labels["channels"] == "llm_inference"
    assert "channels" not in result.target_profile.feature_evidence
    assert "Afterpay" not in fake_llm.prompts[0]
    assert all(document.source_id != "company_pages" for document in result.source_documents)


def test_target_profile_extractor_uses_ir_discovery_instead_of_polygon_homepage(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "business_summary": "e.l.f. Beauty is a multi-brand beauty company.",
            "products": ["beauty products"],
            "customer_segments": [],
            "channels": [],
            "geographies": [],
            "keywords": [],
            "adjacent_categories": [],
            "field_support": {
                "business_summary": ["e.l.f. Beauty is a multi-brand beauty company"],
                "products": ["multi-brand beauty company"],
            },
        }
    )
    fake_ir_discovery = FakeIRPageDiscovery()

    with session_factory() as session:
        extractor = build_test_extractor(
            settings,
            session,
            fake_llm,
            ingestion_service=FakePolygonHomepageIngestionService(),
            ir_page_discovery=fake_ir_discovery,
        )
        result = extractor.build_profile("ELF")

    company_documents = [document for document in result.source_documents if document.source_id == "company_pages"]
    assert [document.url for document in company_documents] == ["https://investor.elfbeauty.com/"]
    assert company_documents[0].metadata["page_role"] == "investor_relations"
    assert fake_ir_discovery.homepage_urls == ["https://www.elfcosmetics.com"]
    assert result.target_profile.feature_evidence["business_summary"][0].url == "https://investor.elfbeauty.com/"


def test_target_profile_extractor_does_not_homepage_fallback_when_ir_discovery_fails(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "business_summary": "e.l.f. Beauty sells cosmetics.",
            "products": ["cosmetics"],
            "customer_segments": [],
            "channels": [],
            "geographies": [],
            "keywords": [],
            "adjacent_categories": [],
            "field_support": {
                "business_summary": ["sells cosmetics"],
                "products": ["sells cosmetics"],
            },
        }
    )

    with session_factory() as session:
        extractor = build_test_extractor(
            settings,
            session,
            fake_llm,
            ingestion_service=FakePolygonHomepageIngestionService(),
            ir_page_discovery=FakeFailedIRPageDiscovery(),
        )
        result = extractor.build_profile("ELF")

    assert all(document.source_id != "company_pages" for document in result.source_documents)
    assert any("company_pages IR discovery failed: status=no_valid_candidate" in warning for warning in result.warnings)


def test_target_profile_extractor_loads_keyword_taxonomy_from_config(tmp_path: Path) -> None:
    taxonomy_path = tmp_path / "keyword_taxonomy.yaml"
    taxonomy_path.write_text(
        "\n".join(
            [
                "version: 7",
                "sic_keywords:",
                '  "2844":',
                "    keywords:",
                "      - color cosmetics",
            ]
        ),
        encoding="utf-8",
    )
    settings = settings_for_tests(tmp_path, keyword_taxonomy_path=taxonomy_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FakeLLMClient(
        {
            "business_summary": "e.l.f. Beauty sells cosmetics through retail and e-commerce.",
            "products": ["cosmetics"],
            "customer_segments": [],
            "channels": [],
            "geographies": [],
            "keywords": [],
            "adjacent_categories": [],
            "field_support": {
                "business_summary": ["sells cosmetics through retail and e-commerce"],
                "products": ["sells cosmetics"],
            },
        }
    )

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, fake_llm)
        result = extractor.build_profile("ELF")

    assert result.target_profile.keywords == ["cosmetics", "color cosmetics"]
    assert result.target_profile.keyword_groups == {
        "verified_products": ["cosmetics"],
        "sic_taxonomy": ["color cosmetics"],
    }
    assert result.extraction_metadata["keyword_taxonomy_version"] == 7


def test_target_profile_extractor_requires_openrouter_key(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path, openrouter_api_key=None)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, FakeLLMClient({}))
        with pytest.raises(TargetProfileExtractionError) as error:
            extractor.build_profile("ELF")

    assert error.value.error_code == "missing_llm_configuration"


def test_target_profile_extractor_retries_invalid_llm_json(tmp_path: Path) -> None:
    settings = settings_for_tests(tmp_path)
    engine = init_db(settings)
    session_factory = create_session_factory(engine)
    fake_llm = FailingLLMClient()

    with session_factory() as session:
        extractor = build_test_extractor(settings, session, fake_llm)
        with pytest.raises(TargetProfileExtractionError) as error:
            extractor.build_profile("ELF")

    assert error.value.error_code == "invalid_llm_json"
    assert fake_llm.calls == 2


def test_target_profile_api_returns_profile_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for_tests(tmp_path)
    monkeypatch.setattr("src.api.main.build_target_profile_extractor", lambda _settings, _session: FakeProfileService())
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/targets/profile?query=ELF")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_profile"]["ticker"] == "ELF"
    assert payload["extraction_metadata"]["extractor_version"] == "test"


def test_build_target_profile_cli_outputs_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = settings_for_tests(tmp_path)
    monkeypatch.setattr("src.cli.get_settings", lambda: settings)
    monkeypatch.setattr("src.cli.build_target_profile_extractor", lambda _settings, _session: FakeProfileService())

    exit_code = cli_main(["build-target-profile", "ELF"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["target_profile"]["ticker"] == "ELF"


def build_test_extractor(
    settings: Settings,
    session,
    llm_client,
    ingestion_service=None,
    ir_page_discovery=None,
    edgar_client=None,
) -> TargetProfileExtractor:
    strategy = DataSourceStrategy.from_settings(settings)
    cache = SourceCache(session)
    return TargetProfileExtractor(
        settings=settings,
        strategy=strategy,
        ingestion_service=ingestion_service or FakeIngestionService(),
        source_cache=cache,
        profile_cache=TargetProfileCache(session),
        edgar_client=edgar_client or FakeProfileSecClient(),
        llm_client=llm_client,
        ir_page_discovery=ir_page_discovery,
    )


def sample_target() -> ResolvedTarget:
    return ResolvedTarget(
        canonical_name="e.l.f. Beauty, Inc.",
        ticker="ELF",
        cik="0001600033",
        exchange="NYSE",
        sic="2844",
        resolution_confidence=1.0,
        matched_input="ELF",
        source_provenance=["fixture"],
    )


def sample_filing(form: str) -> FilingMetadata:
    return FilingMetadata(
        form=form,
        filing_date="2025-05-30",
        accession_number="0000923796-25-000001",
        period_of_report="2025-03-31",
        url="https://www.sec.gov/Archives/edgar/data/1600033/000092379625000001/0000923796-25-000001-index.html",
    )


def sample_profile_result() -> TargetProfileExtractionResult:
    profile = TargetProfile(
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
        keyword_groups={"llm_fallback": ["beauty"]},
        adjacent_categories=["personal care"],
    )
    return TargetProfileExtractionResult(
        target_profile=profile,
        source_documents=[],
        warnings=[],
        extraction_metadata={"extractor_version": "test"},
    )


class FakeEdgarTextModule:
    def __init__(
        self,
        text: str = "",
        markdown: str = "",
        text_error: Exception | None = None,
        report_object: Any | None = None,
    ) -> None:
        self.text = text
        self.markdown = markdown
        self.text_error = text_error
        self.report_object = report_object
        self.filing_calls: list[str] = []
        self.obj_calls = 0

    def set_identity(self, _identity: str) -> None:
        return None

    def Filing(self, **kwargs):
        self.filing_calls.append(kwargs["accession_no"])
        return FakeEdgarTextFiling(self, self.text, self.markdown, self.text_error, self.report_object)


class FakeEdgarTextFiling:
    def __init__(
        self,
        module: FakeEdgarTextModule,
        text: str,
        markdown: str,
        text_error: Exception | None,
        report_object: Any | None,
    ) -> None:
        self._module = module
        self._text = text
        self._markdown = markdown
        self._text_error = text_error
        self._report_object = report_object

    def obj(self):
        self._module.obj_calls += 1
        return self._report_object

    def text(self) -> str:
        if self._text_error:
            raise self._text_error
        return self._text

    def markdown(self) -> str:
        return self._markdown

    def full_text_submission(self) -> str:
        return ""


class FakeTenKReport:
    items = ["Item 1", "Item 1A", "Item 7"]

    def __init__(self, business: str = "", management_discussion: str = "", risk_factors: str = "") -> None:
        self.business = business
        self.management_discussion = management_discussion
        self.risk_factors = risk_factors


class FakeLLMClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
        self.schema_names: list[str] = []
        self.json_schemas: list[dict[str, Any] | None] = []
        self.system_prompts: list[str | None] = []
        self.source_business_types: list[str] = []
        self.prompts: list[str] = []

    def generate_json(
        self,
        _prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(_prompt)
        self.schema_names.append(schema_name)
        self.json_schemas.append(json_schema)
        self.system_prompts.append(system_prompt)
        self.source_business_types.append(source_business_type)
        return self.payload


class FakeWebSearchJSONClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompts: list[str] = []
        self.source_business_types: list[str] = []

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
        return self.payload


class FailingLLMClient:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(
        self,
        _prompt: str,
        _schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        self.calls += 1
        raise LLMResponseError("invalid json")


class FakeIngestionService:
    def ingest(self, _query: str) -> TargetIngestionResult:
        target = sample_target()
        filing = sample_filing("10-K")
        retrieved_at = datetime.now(UTC)
        text = "ITEM 1. Business e.l.f. Beauty sells cosmetics through retail and e-commerce to retail consumers in the United States."
        return TargetIngestionResult(
            target=target,
            filings=[filing],
            source_documents=[
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.identity_resolution",
                    source_type=SourceType.sec_company_mapping,
                    source_strength=SourceStrength.A,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url="https://www.sec.gov/files/company_tickers_exchange.json",
                    metadata=target.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.business_description",
                    source_type=SourceType.sec_filing,
                    source_strength=SourceStrength.B,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url=filing.url,
                    filing_accession=filing.accession_number,
                    raw_text=text,
                    metadata=filing.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
            ],
        )


class FakeStructuredSectionsIngestionService:
    def ingest(self, _query: str) -> TargetIngestionResult:
        target = sample_target()
        filing = sample_filing("10-K")
        retrieved_at = datetime.now(UTC)
        sections = {
            "business": "ITEM 1. Business e.l.f. Beauty sells cosmetics through retail and e-commerce.",
            "management_discussion": (
                "ITEM 7. Management's Discussion and Analysis We intend to invest in digital commerce "
                "and pursue strategic acquisitions."
            ),
        }
        return TargetIngestionResult(
            target=target,
            filings=[filing],
            source_documents=[
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.business_description",
                    source_type=SourceType.sec_filing,
                    source_strength=SourceStrength.B,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url=filing.url,
                    filing_accession=filing.accession_number,
                    raw_text=sections["business"],
                    metadata={
                        **filing.model_dump(mode="json"),
                        "text_retrieval_method": "edgartools:Filing.obj",
                        "text_scope": "item_1_business",
                        "structured_sections": sections,
                    },
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
            ],
        )


class FakeNoisyCompanyPageIngestionService:
    def ingest(self, _query: str) -> TargetIngestionResult:
        target = sample_target()
        filing = sample_filing("10-K")
        retrieved_at = datetime.now(UTC)
        return TargetIngestionResult(
            target=target,
            filings=[filing],
            source_documents=[
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.identity_resolution",
                    source_type=SourceType.sec_company_mapping,
                    source_strength=SourceStrength.A,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url="https://www.sec.gov/files/company_tickers_exchange.json",
                    metadata=target.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.business_description",
                    source_type=SourceType.sec_filing,
                    source_strength=SourceStrength.B,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url=filing.url,
                    filing_accession=filing.accession_number,
                    raw_text="ITEM 1. Business e.l.f. Beauty sells cosmetics.",
                    metadata=filing.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
                SourceDocument(
                    source_id="company_pages",
                    source_dimension="seller_profile.business_description",
                    source_type=SourceType.company_page,
                    source_strength=SourceStrength.A,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url="https://www.elfcosmetics.com",
                    raw_text="Afterpay: Shop Now, Pay in 4 interest-free payments!",
                    metadata={"page_role": "homepage"},
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
            ],
        )


class FakePolygonHomepageIngestionService:
    def ingest(self, _query: str) -> TargetIngestionResult:
        target = sample_target()
        filing = sample_filing("10-K")
        retrieved_at = datetime.now(UTC)
        return TargetIngestionResult(
            target=target,
            filings=[filing],
            source_documents=[
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.identity_resolution",
                    source_type=SourceType.sec_company_mapping,
                    source_strength=SourceStrength.A,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url="https://www.sec.gov/files/company_tickers_exchange.json",
                    metadata=target.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
                SourceDocument(
                    source_id="edgar",
                    source_dimension="seller_profile.business_description",
                    source_type=SourceType.sec_filing,
                    source_strength=SourceStrength.B,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url=filing.url,
                    filing_accession=filing.accession_number,
                    raw_text="ITEM 1. Business e.l.f. Beauty sells cosmetics.",
                    metadata=filing.model_dump(mode="json"),
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
                SourceDocument(
                    source_id="polygon",
                    source_dimension="seller_profile.exchange_profile",
                    source_type=SourceType.exchange_profile,
                    source_strength=SourceStrength.B,
                    target_cik=target.cik,
                    target_ticker=target.ticker,
                    url="https://api.polygon.io/v3/reference/tickers/ELF",
                    metadata={"homepage_url": "https://www.elfcosmetics.com"},
                    retrieved_at=retrieved_at,
                    expires_at=retrieved_at + timedelta(hours=24),
                ),
            ],
        )


class FakeIRPageDiscovery:
    def __init__(self) -> None:
        self.homepage_urls: list[str | None] = []

    def discover(
        self,
        target: ResolvedTarget,
        homepage_url: str | None,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> IRPageDiscoveryResult:
        self.homepage_urls.append(homepage_url)
        retrieved_at = datetime.now(UTC)
        return IRPageDiscoveryResult(
            document=SourceDocument(
                source_id="company_pages",
                source_dimension=source_dimension,
                source_type=SourceType.company_page,
                source_strength=source_strength,
                target_cik=target.cik,
                target_ticker=target.ticker,
                url="https://investor.elfbeauty.com/",
                raw_text="Investor Relations SEC Filings e.l.f. Beauty is a multi-brand beauty company.",
                metadata={
                    "page_role": "investor_relations",
                    "discovered_from": "llm_web_search",
                    "validated_signals": ["investor relations", "company_name_compact_match"],
                },
                retrieved_at=retrieved_at,
                expires_at=retrieved_at + timedelta(hours=ttl_hours),
            ),
            status="selected",
            selected_url="https://investor.elfbeauty.com/",
            candidates_checked=1,
        )


class FakeFailedIRPageDiscovery:
    def discover(
        self,
        target: ResolvedTarget,
        homepage_url: str | None,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> IRPageDiscoveryResult:
        return IRPageDiscoveryResult(
            document=None,
            status="no_valid_candidate",
            candidates_checked=1,
            rejection_reasons=["https://www.elfcosmetics.com: storefront/homepage signals dominate"],
        )


class FakeProfileSecClient:
    def fetch_filing_text_document(self, *args, **kwargs):
        if kwargs.get("text_scope") == "company_strategy":
            target = args[0]
            filing = args[1]
            ttl_hours = args[2]
            retrieved_at = datetime.now(UTC)
            return SourceDocument(
                source_id="edgar",
                source_dimension=kwargs.get("source_dimension"),
                source_type=SourceType.sec_filing,
                source_strength=kwargs.get("source_strength", SourceStrength.B),
                target_cik=target.cik,
                target_ticker=target.ticker,
                url=filing.url,
                filing_accession=filing.accession_number,
                raw_text=(
                    "ITEM 7. Management's Discussion and Analysis We intend to invest in digital commerce "
                    "and pursue strategic acquisitions."
                ),
                metadata={
                    **filing.model_dump(mode="json"),
                    "text_scope": "item_7_mdna",
                },
                retrieved_at=retrieved_at,
                expires_at=retrieved_at + timedelta(hours=ttl_hours),
            )
        raise AssertionError("text document should already be present in fixture")


class FailingProfileSecClient:
    def fetch_filing_text_document(self, *args, **kwargs):
        raise AssertionError("structured sections should be reused without fetching SEC text again")


class FakeProfileService:
    def build_profile(self, _query: str) -> TargetProfileExtractionResult:
        return sample_profile_result()
