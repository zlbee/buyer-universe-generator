from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

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
from src.repositories.database import create_session_factory, init_db
from src.repositories.source_cache import SourceCache
from src.repositories.target_profile_cache import TargetProfileCache
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy


def settings_for_tests(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        "cache_dir": tmp_path / "cache",
        "datasource_policy_path": Path("config/datasources.yaml"),
        "edgar_identity": "buyer-universe-generator/0.1 contact@example.com",
        "openrouter_api_key": "openrouter-test-key",
        "polygon_api_key": None,
        "news_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_sec_filing_text_document_uses_edgartools_text(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(text="ITEM 1. Business e.l.f. Beauty sells cosmetics. ITEM 1A. Risk Factors")
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    document = client.fetch_filing_text_document(
        sample_target(),
        sample_filing("10-K"),
        ttl_hours=24,
        source_strength=SourceStrength.B,
        source_dimension="seller_profile.business_description",
    )

    assert document.raw_text == "ITEM 1. Business e.l.f. Beauty sells cosmetics."
    assert document.source_dimension == "seller_profile.business_description"
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.text"
    assert fake_edgar.filing_calls == ["0000923796-25-000001"]


def test_sec_filing_text_document_falls_back_to_markdown(tmp_path: Path) -> None:
    fake_edgar = FakeEdgarTextModule(text_error=RuntimeError("text unavailable"), markdown="ITEM 1. Business cosmetics brand")
    client = SecEdgarClient(settings_for_tests(tmp_path), edgar_module=fake_edgar)

    document = client.fetch_filing_text_document(sample_target(), sample_filing("10-K"), ttl_hours=24)

    assert document.raw_text == "ITEM 1. Business cosmetics brand"
    assert document.metadata["text_retrieval_method"] == "edgartools:Filing.markdown"


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
        },
    )
    service = object.__new__(SourceIngestionService)

    restored_filings = service._filings_from_documents([document])

    assert restored_filings[0].accession_number == filing.accession_number
    assert restored_filings[0].text_retrieval_method == "edgartools:Filing.text"
    assert restored_filings[0].text_scope == "item_1_business"
    assert restored_filings[0].raw_text_char_count == 394227
    assert restored_filings[0].cached_text_char_count == 18


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
            "products": ["cosmetics"],
            "customer_segments": ["retail consumers"],
            "channels": ["retail", "e-commerce"],
            "geographies": ["United States"],
            "keywords": ["cosmetics", "beauty"],
            "adjacent_categories": ["personal care"],
            "field_support": {
                "business_summary": ["sells cosmetics through retail and e-commerce"],
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
    assert profile.products == ["cosmetics"]
    assert profile.feature_labels["business_summary"] == "verified_fact"
    assert profile.feature_labels["keywords"] == "derived_keyword"
    assert profile.feature_labels["adjacent_categories"] == "llm_inference"
    assert profile.feature_evidence["business_summary"][0].source_dimension == "seller_profile.business_description"
    assert first_result.extraction_metadata["cache_hit"] is False
    assert second_result.extraction_metadata["cache_hit"] is True
    assert fake_llm.calls == 1
    assert fake_llm.schema_names == ["TargetProfileFeatureExtraction"]
    assert fake_llm.system_prompts[0].startswith("You are extracting a target company profile")
    assert fake_llm.json_schemas[0]["additionalProperties"] is False
    assert "field_support" in fake_llm.json_schemas[0]["required"]
    assert fake_llm.json_schemas[0]["properties"]["business_summary"]["anyOf"][0]["type"] == "string"
    assert fake_llm.json_schemas[0]["properties"]["field_support"]["additionalProperties"] is False


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
    assert result.target_profile.keywords == ["beauty"]
    assert result.target_profile.feature_labels["business_summary"] == "verified_fact"


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


def build_test_extractor(settings: Settings, session, llm_client) -> TargetProfileExtractor:
    strategy = DataSourceStrategy.from_settings(settings)
    cache = SourceCache(session)
    return TargetProfileExtractor(
        settings=settings,
        strategy=strategy,
        ingestion_service=FakeIngestionService(),
        source_cache=cache,
        profile_cache=TargetProfileCache(session),
        edgar_client=FakeProfileSecClient(),
        llm_client=llm_client,
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
        adjacent_categories=["personal care"],
    )
    return TargetProfileExtractionResult(
        target_profile=profile,
        source_documents=[],
        warnings=[],
        extraction_metadata={"extractor_version": "test"},
    )


class FakeEdgarTextModule:
    def __init__(self, text: str = "", markdown: str = "", text_error: Exception | None = None) -> None:
        self.text = text
        self.markdown = markdown
        self.text_error = text_error
        self.filing_calls: list[str] = []

    def set_identity(self, _identity: str) -> None:
        return None

    def Filing(self, **kwargs):
        self.filing_calls.append(kwargs["accession_no"])
        return FakeEdgarTextFiling(self.text, self.markdown, self.text_error)


class FakeEdgarTextFiling:
    def __init__(self, text: str, markdown: str, text_error: Exception | None) -> None:
        self._text = text
        self._markdown = markdown
        self._text_error = text_error

    def text(self) -> str:
        if self._text_error:
            raise self._text_error
        return self._text

    def markdown(self) -> str:
        return self._markdown

    def full_text_submission(self) -> str:
        return ""


class FakeLLMClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
        self.schema_names: list[str] = []
        self.json_schemas: list[dict[str, Any] | None] = []
        self.system_prompts: list[str | None] = []

    def generate_json(
        self,
        _prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        self.schema_names.append(schema_name)
        self.json_schemas.append(json_schema)
        self.system_prompts.append(system_prompt)
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


class FakeProfileSecClient:
    def fetch_filing_text_document(self, *_args, **_kwargs):
        raise AssertionError("text document should already be present in fixture")


class FakeProfileService:
    def build_profile(self, _query: str) -> TargetProfileExtractionResult:
        return sample_profile_result()
