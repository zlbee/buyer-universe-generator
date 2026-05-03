from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from src.api.main import create_app
from src.config import Settings
from src.domain import (
    BuyerType,
    CandidateHit,
    Evidence,
    FeatureLabel,
    LongListCandidate,
    PipelineRun,
    SourceStrength,
    TargetProfile,
)
from src.repositories.database import init_db


def test_phase0_foundation_smoke(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'buyer_universe.db'}",
        cache_dir=tmp_path / "cache",
    )

    evidence = Evidence(
        claim="The target files annual reports with the SEC.",
        source_type="sec_filing",
        source_strength=SourceStrength.A,
        filing_accession="0000000000-26-000001",
        quote_or_snippet="Annual report filing metadata",
        verified_fact=True,
    )
    profile = TargetProfile(
        target_id="0000000000",
        name="Example Target Inc.",
        ticker="EXTG",
        cik="0000000000",
        exchange="Nasdaq",
        sic="1234",
        business_summary="Example business summary.",
        company_strategy="Example SEC-backed strategy.",
        products=["example product"],
        customer_segments=["enterprise customers"],
        channels=["direct sales"],
        geographies=["United States"],
        size_metrics={"revenue_usd": 100_000_000},
        keywords=["example"],
        adjacent_categories=["example adjacent category"],
        feature_labels={"business_summary": FeatureLabel.verified_fact},
        evidence=[evidence],
    )
    hit = CandidateHit(
        candidate_name="Example Buyer Inc.",
        buyer_type=BuyerType.strategic,
        retriever_name="SameSicRetriever",
        source_path=["same_sic"],
        fit_reason="Shares the target SIC code.",
        evidence=[evidence],
        confidence=0.75,
    )
    candidate = LongListCandidate(
        canonical_name=hit.candidate_name,
        buyer_type=hit.buyer_type,
        ticker="EXBY",
        source_paths=hit.source_path,
        hit_count=1,
        initial_score=72.5,
        fit_reasons=[hit.fit_reason],
        evidence=hit.evidence,
    )
    run = PipelineRun(
        run_id="phase0-smoke",
        input_query="EXTG",
        target_profile=profile,
        long_list=[candidate],
    )

    serialized = run.model_dump(mode="json")
    assert serialized["target_profile"]["ticker"] == "EXTG"
    assert serialized["long_list"][0]["canonical_name"] == "Example Buyer Inc."

    engine = init_db(settings)
    with engine.connect() as connection:
        assert connection.execute(text("select 1")).scalar_one() == 1

    assert settings.cache_dir.exists()
    assert "pipeline_runs" in inspect(engine).get_table_names()
    assert "target_resolutions" in inspect(engine).get_table_names()
    assert "source_documents" in inspect(engine).get_table_names()
    assert "llm_interactions" in inspect(engine).get_table_names()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
