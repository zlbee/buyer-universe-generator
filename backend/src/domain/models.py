"""Pydantic models shared across API, pipeline, persistence, and exports."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictBaseModel(BaseModel):
    """Base model that rejects unknown fields to keep pipeline contracts explicit."""

    model_config = ConfigDict(extra="forbid")


class BuyerType(str, Enum):
    strategic = "strategic"
    financial = "financial"


class SourceStrength(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class SourceType(str, Enum):
    sec_filing = "sec_filing"
    sec_company_mapping = "sec_company_mapping"
    exchange_profile = "exchange_profile"
    company_page = "company_page"
    news_article = "news_article"
    source_policy = "source_policy"


class FeatureLabel(str, Enum):
    verified_fact = "verified_fact"
    derived_keyword = "derived_keyword"
    llm_inference = "llm_inference"


class PipelineRunStatus(str, Enum):
    created = "created"
    running = "running"
    completed = "completed"
    failed = "failed"


class ResolvedTarget(StrictBaseModel):
    """Canonical target identity resolved from public data sources."""

    canonical_name: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    cik: str = Field(min_length=1)
    exchange: str | None = None
    sic: str | None = None
    resolution_confidence: float = Field(ge=0.0, le=1.0)
    matched_input: str = Field(min_length=1)
    source_provenance: list[str] = Field(default_factory=list)


class FilingMetadata(StrictBaseModel):
    """SEC filing metadata cached for later feature extraction."""

    form: str = Field(min_length=1)
    filing_date: str | None = None
    accession_number: str = Field(min_length=1)
    period_of_report: str | None = None
    url: str | None = None
    source_type: SourceType = SourceType.sec_filing
    text_retrieval_method: str | None = None
    text_scope: str | None = None
    raw_text_char_count: int | None = Field(default=None, ge=0)
    cached_text_char_count: int | None = Field(default=None, ge=0)


class SourceDocument(StrictBaseModel):
    """Cached source material or metadata collected during ingestion."""

    source_id: str = Field(min_length=1)
    source_dimension: str | None = None
    source_type: SourceType
    source_strength: SourceStrength
    target_cik: str | None = None
    target_ticker: str | None = None
    url: str | None = None
    filing_accession: str | None = None
    raw_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_reference_or_metadata(self) -> "SourceDocument":
        if not self.url and not self.filing_accession and not self.metadata:
            raise ValueError("SourceDocument requires url, filing_accession, or metadata")
        return self


class TargetIngestionResult(StrictBaseModel):
    """Phase 1 output containing resolved target identity and cached source metadata."""

    target: ResolvedTarget
    filings: list[FilingMetadata] = Field(default_factory=list)
    source_documents: list[SourceDocument] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DataSourceDimensionConfig(StrictBaseModel):
    """Dimension-scoped support policy for one data source."""

    source_strength: SourceStrength
    field_coverage: list[str] = Field(default_factory=list)
    rationale: str | None = None


class DataSourceConfig(StrictBaseModel):
    """Provider-level data-source policy that avoids dimension-specific scoring."""

    provider: str = Field(min_length=1)
    enabled: bool = True
    required: bool = False
    api_key_env: str | None = None
    cache_ttl_hours: int = Field(default=24, ge=0)
    dimensions: dict[str, DataSourceDimensionConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_dimension_policies(self) -> "DataSourceConfig":
        if not self.dimensions:
            raise ValueError("DataSourceConfig requires at least one dimension policy")
        return self


class DataSourceUseCaseConfig(StrictBaseModel):
    """A concrete source selection for a specific pipeline use case."""

    source_id: str = Field(min_length=1)
    dimension: str = Field(min_length=1)


class DataSourcePolicy(StrictBaseModel):
    """Validated data-source selection policy loaded from YAML."""

    version: int = 1
    sources: dict[str, DataSourceConfig]
    use_cases: dict[str, list[DataSourceUseCaseConfig]]

    @model_validator(mode="after")
    def validate_use_case_references(self) -> "DataSourcePolicy":
        for use_case, selections in self.use_cases.items():
            for selection in selections:
                source = self.sources.get(selection.source_id)
                if source is None:
                    raise ValueError(f"use case {use_case} references unknown source {selection.source_id}")
                if selection.dimension not in source.dimensions:
                    raise ValueError(
                        f"use case {use_case} references unknown dimension "
                        f"{selection.source_id}.{selection.dimension}"
                    )
        return self


class Evidence(StrictBaseModel):
    """A citation-backed claim used to defend target features or buyer candidates."""

    claim: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_dimension: str | None = None
    source_strength: SourceStrength
    url: str | None = None
    filing_accession: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    quote_or_snippet: str | None = None
    verified_fact: bool = True

    @model_validator(mode="after")
    def require_source_reference(self) -> "Evidence":
        # Every evidence item must be independently traceable to a URL or SEC filing.
        if not self.url and not self.filing_accession:
            raise ValueError("Evidence requires either url or filing_accession")
        return self


class TargetProfile(StrictBaseModel):
    """Minimum target profile required to anchor long-list candidate retrieval."""

    target_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    cik: str = Field(min_length=1)
    exchange: str | None = None
    sic: str | None = None
    business_summary: str | None = None
    products: list[str] = Field(default_factory=list)
    customer_segments: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    size_metrics: dict[str, Any] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    keyword_groups: dict[str, list[str]] = Field(default_factory=dict)
    adjacent_categories: list[str] = Field(default_factory=list)
    feature_labels: dict[str, FeatureLabel] = Field(default_factory=dict)
    feature_evidence: dict[str, list[Evidence]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)


class TargetProfileExtractionResult(StrictBaseModel):
    """Phase 2 output containing the retrieval-ready target profile and provenance."""

    target_profile: TargetProfile
    source_documents: list[SourceDocument] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateHit(StrictBaseModel):
    """Raw buyer candidate emitted by one retriever before dedupe and filtering."""

    candidate_name: str = Field(min_length=1)
    buyer_type: BuyerType
    retriever_name: str = Field(min_length=1)
    source_path: list[str] = Field(default_factory=list)
    fit_reason: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    pending_verification: bool = False

    @model_validator(mode="after")
    def require_evidence_or_pending_status(self) -> "CandidateHit":
        # Retrievers may emit unverified leads, but they must mark them explicitly.
        if not self.evidence and not self.pending_verification:
            raise ValueError("CandidateHit requires evidence or pending_verification=True")
        return self


class LongListCandidate(StrictBaseModel):
    """Canonical buyer candidate after normalization, filtering, and pre-scoring."""

    canonical_name: str = Field(min_length=1)
    buyer_type: BuyerType
    ticker: str | None = None
    cik: str | None = None
    domain: str | None = None
    source_paths: list[str] = Field(default_factory=list)
    hit_count: int = Field(default=1, ge=1)
    initial_score: float = Field(default=0.0, ge=0.0, le=100.0)
    fit_reasons: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_formal_long_list_evidence(self) -> "LongListCandidate":
        # Formal long-list entries must be defensible; weak leads belong in pending queues.
        if not self.evidence:
            raise ValueError("LongListCandidate requires at least one evidence item")
        return self


class PipelineRun(StrictBaseModel):
    """Serializable run envelope used by the API, CLI, and persistence layer."""

    run_id: str = Field(min_length=1)
    input_query: str = Field(min_length=1)
    status: PipelineRunStatus = PipelineRunStatus.created
    target_profile: TargetProfile | None = None
    long_list: list[LongListCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
