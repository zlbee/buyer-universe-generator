"""Phase 2 target feature extraction from cached public source material."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.config import Settings
from src.domain import (
    Evidence,
    FeatureLabel,
    FilingMetadata,
    SourceDocument,
    SourceStrength,
    SourceType,
    TargetIngestionResult,
    TargetProfile,
    TargetProfileExtractionResult,
)
from src.llm import LLMClient, LLMResponseError, MissingLLMConfigurationError
from src.pipelines.source_ingestion import SourceIngestionService
from src.repositories.source_cache import SourceCache
from src.repositories.target_profile_cache import TargetProfileCache
from src.sources.company_pages import CompanyPageClient
from src.sources.sec import SecEdgarClient
from src.sources.strategy import DataSourceStrategy

logger = logging.getLogger(__name__)


class TargetProfileExtractionError(RuntimeError):
    """Structured Phase 2 failure that API and CLI layers can serialize."""

    def __init__(self, message: str, error_code: str, status_code: int = 422, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}


class LLMTargetFeatureOutput(BaseModel):
    """Expected JSON shape returned by the target profile extraction prompt."""

    model_config = ConfigDict(extra="ignore")

    business_summary: str | None = None
    products: list[str] = Field(default_factory=list)
    customer_segments: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    adjacent_categories: list[str] = Field(default_factory=list)
    field_support: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator(
        "products",
        "customer_segments",
        "channels",
        "geographies",
        "keywords",
        "adjacent_categories",
        mode="before",
    )
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        # JSON-mode providers sometimes collapse a one-item array into a scalar string.
        return _coerce_string_list(value)

    @field_validator("field_support", mode="before")
    @classmethod
    def normalize_field_support(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return {}

        normalized: dict[str, list[str]] = {}
        for key, snippets in value.items():
            if not isinstance(key, str):
                continue
            normalized_snippets = _coerce_string_list(snippets)
            if normalized_snippets:
                normalized[key] = normalized_snippets
        return normalized


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []

    normalized: list[str] = []
    for item in values:
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


_LLM_EXTRACTABLE_FIELDS = (
    "business_summary",
    "products",
    "customer_segments",
    "channels",
    "geographies",
    "keywords",
    "adjacent_categories",
)


def target_feature_json_schema() -> dict[str, Any]:
    string_array_schema = {"type": "array", "items": {"type": "string"}}

    # Keep the provider-facing schema stricter than the repair-tolerant local model.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [*_LLM_EXTRACTABLE_FIELDS, "field_support"],
        "properties": {
            "business_summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "products": string_array_schema,
            "customer_segments": string_array_schema,
            "channels": string_array_schema,
            "geographies": string_array_schema,
            "keywords": string_array_schema,
            "adjacent_categories": string_array_schema,
            "field_support": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_LLM_EXTRACTABLE_FIELDS),
                "properties": {field_name: string_array_schema for field_name in _LLM_EXTRACTABLE_FIELDS},
            },
        },
    }


def target_profile_llm_system_prompt(schema_name: str) -> str:
    return (
        "You are extracting a target company profile for M&A buyer long-list retrieval. "
        f"Return only valid JSON for schema {schema_name}; do not include prose."
    )


class TargetProfileExtractor:
    """Builds a retrieval-ready TargetProfile from Phase 1 source metadata and LLM extraction."""

    llm_schema_name = "TargetProfileFeatureExtraction"

    def __init__(
        self,
        settings: Settings,
        strategy: DataSourceStrategy,
        ingestion_service: SourceIngestionService,
        source_cache: SourceCache,
        profile_cache: TargetProfileCache,
        edgar_client: SecEdgarClient,
        llm_client: LLMClient,
        company_page_client: CompanyPageClient | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.ingestion_service = ingestion_service
        self.source_cache = source_cache
        self.profile_cache = profile_cache
        self.edgar_client = edgar_client
        self.llm_client = llm_client
        self.company_page_client = company_page_client

    def build_profile(self, query: str) -> TargetProfileExtractionResult:
        logger.info("TargetProfile extraction started: query=%s", query)
        ingestion = self.ingestion_service.ingest(query)
        warnings = list(ingestion.warnings)
        source_documents = list(ingestion.source_documents)
        logger.info(
            "TargetProfile target resolved: query=%s ticker=%s cik=%s source_documents=%s warnings=%s",
            query,
            ingestion.target.ticker,
            ingestion.target.cik,
            len(source_documents),
            len(warnings),
        )

        source_documents = self._ensure_filing_text(ingestion, source_documents, warnings)
        source_documents = self._ensure_company_page(ingestion, source_documents, warnings)
        logger.info(
            "TargetProfile sources prepared: ticker=%s source_documents=%s text_documents=%s raw_text_chars=%s warnings=%s",
            ingestion.target.ticker,
            len(source_documents),
            len([document for document in source_documents if document.raw_text]),
            sum(len(document.raw_text or "") for document in source_documents),
            len(warnings),
        )

        if not self.settings.openrouter_api_key:
            logger.warning("TargetProfile extraction missing OpenRouter key: query=%s", query)
            raise TargetProfileExtractionError(
                "BUG_OPENROUTER_API_KEY is required for target profile extraction",
                error_code="missing_llm_configuration",
                status_code=400,
            )

        source_fingerprint = fingerprint_source_documents(source_documents)
        cached = self.profile_cache.get_valid(
            target_cik=ingestion.target.cik,
            extractor_version=self.settings.target_profile_extractor_version,
            source_fingerprint=source_fingerprint,
        )
        if cached:
            logger.info(
                "TargetProfile cache hit: ticker=%s cik=%s source_fingerprint=%s",
                ingestion.target.ticker,
                ingestion.target.cik,
                source_fingerprint,
            )
            return cached.model_copy(
                update={
                    "extraction_metadata": {
                        **cached.extraction_metadata,
                        "cache_hit": True,
                    }
                }
            )

        logger.info(
            "TargetProfile cache miss: ticker=%s cik=%s source_fingerprint=%s",
            ingestion.target.ticker,
            ingestion.target.cik,
            source_fingerprint,
        )
        llm_output = self._extract_with_llm(ingestion, source_documents)
        result = self._assemble_profile_result(ingestion, source_documents, warnings, llm_output, source_fingerprint)
        self.profile_cache.save(
            target_cik=ingestion.target.cik,
            target_ticker=ingestion.target.ticker,
            extractor_version=self.settings.target_profile_extractor_version,
            source_fingerprint=source_fingerprint,
            result=result,
            ttl_hours=self.settings.target_profile_cache_ttl_hours,
        )
        logger.info(
            "TargetProfile extraction completed: ticker=%s fields=%s evidence_items=%s warnings=%s",
            ingestion.target.ticker,
            _populated_llm_fields(llm_output),
            len(result.target_profile.evidence),
            len(result.warnings),
        )
        return result

    def _ensure_filing_text(
        self,
        ingestion: TargetIngestionResult,
        source_documents: list[SourceDocument],
        warnings: list[str],
    ) -> list[SourceDocument]:
        if any(document.raw_text and document.source_dimension == "seller_profile.business_description" for document in source_documents):
            return source_documents

        filing = _preferred_text_filing(ingestion.filings)
        if not filing:
            warnings.append("target profile extraction skipped SEC text: no 10-K or 10-Q filing metadata available")
            return source_documents

        edgar_source = self.strategy.selected_source("seller_profile_business_description", "edgar", include_disabled=True)
        try:
            document = self.edgar_client.fetch_filing_text_document(
                ingestion.target,
                filing,
                self.strategy.cache_ttl_hours("edgar"),
                source_strength=edgar_source.source_strength if edgar_source else SourceStrength.B,
                source_dimension=edgar_source.dimension_id if edgar_source else "seller_profile.business_description",
            )
        except Exception as error:
            warnings.append(f"target profile extraction skipped SEC text: {error}")
            return source_documents

        self.source_cache.save_document(document)
        return [*source_documents, document]

    def _ensure_company_page(
        self,
        ingestion: TargetIngestionResult,
        source_documents: list[SourceDocument],
        warnings: list[str],
    ) -> list[SourceDocument]:
        if any(document.source_id == "company_pages" for document in source_documents):
            return source_documents

        homepage_url = _homepage_url_from_polygon(source_documents)
        if not homepage_url:
            return source_documents

        company_page_source = self.strategy.selected_source(
            "seller_profile_business_description",
            "company_pages",
            include_disabled=True,
        )
        if not company_page_source:
            return source_documents
        if not company_page_source.enabled:
            warnings.append(f"company_pages disabled: {company_page_source.disabled_reason}")
            return source_documents
        if not self.company_page_client:
            warnings.append("company_pages disabled: adapter unavailable")
            return source_documents

        try:
            document = self.company_page_client.fetch_official_page(
                ingestion.target,
                homepage_url,
                self.strategy.cache_ttl_hours("company_pages"),
                source_strength=company_page_source.source_strength,
                source_dimension=company_page_source.dimension_id,
            )
        except Exception as error:
            warnings.append(f"company_pages fetch failed: {error}")
            return source_documents

        if not document:
            return source_documents
        self.source_cache.save_document(document)
        return [*source_documents, document]

    def _extract_with_llm(
        self,
        ingestion: TargetIngestionResult,
        source_documents: list[SourceDocument],
    ) -> LLMTargetFeatureOutput:
        prompt = _build_extraction_prompt(ingestion, source_documents, self.settings.llm_max_input_chars)
        last_error: Exception | None = None
        max_attempts = 2
        logger.info(
            "TargetProfile LLM extraction prepared: ticker=%s schema=%s prompt_chars=%s max_attempts=%s",
            ingestion.target.ticker,
            self.llm_schema_name,
            len(prompt),
            max_attempts,
        )
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "TargetProfile LLM extraction attempt started: ticker=%s attempt=%s/%s",
                    ingestion.target.ticker,
                    attempt,
                    max_attempts,
                )
                payload = self.llm_client.generate_json(
                    prompt,
                    self.llm_schema_name,
                    target_feature_json_schema(),
                    system_prompt=target_profile_llm_system_prompt(self.llm_schema_name),
                )
                logger.info(
                    "TargetProfile LLM extraction attempt returned payload: ticker=%s attempt=%s/%s top_level_keys=%s",
                    ingestion.target.ticker,
                    attempt,
                    max_attempts,
                    sorted(str(key) for key in payload.keys()),
                )
                output = LLMTargetFeatureOutput.model_validate(payload)
                logger.info(
                    "TargetProfile LLM extraction attempt validated: ticker=%s attempt=%s/%s populated_fields=%s",
                    ingestion.target.ticker,
                    attempt,
                    max_attempts,
                    _populated_llm_fields(output),
                )
                return output
            except MissingLLMConfigurationError as error:
                logger.warning("TargetProfile LLM extraction configuration error: ticker=%s error=%s", ingestion.target.ticker, error)
                raise TargetProfileExtractionError(str(error), "missing_llm_configuration", status_code=400) from error
            except LLMResponseError as error:
                logger.warning(
                    "TargetProfile LLM response error: ticker=%s attempt=%s/%s error_type=%s error=%s",
                    ingestion.target.ticker,
                    attempt,
                    max_attempts,
                    type(error).__name__,
                    error,
                )
                last_error = error
            except ValidationError as error:
                logger.warning(
                    "TargetProfile LLM payload validation error: ticker=%s attempt=%s/%s errors=%s",
                    ingestion.target.ticker,
                    attempt,
                    max_attempts,
                    _preview_validation_errors(error),
                )
                last_error = error

        logger.error(
            "TargetProfile LLM extraction failed after retries: ticker=%s attempts=%s final_error_type=%s final_error=%s",
            ingestion.target.ticker,
            max_attempts,
            type(last_error).__name__ if last_error else "unknown",
            last_error if last_error else "unknown",
        )
        raise TargetProfileExtractionError(
            "LLM did not return valid TargetProfile extraction JSON after retry",
            error_code="invalid_llm_json",
            status_code=502,
            details={"error": str(last_error) if last_error else "unknown"},
        )

    def _assemble_profile_result(
        self,
        ingestion: TargetIngestionResult,
        source_documents: list[SourceDocument],
        warnings: list[str],
        llm_output: LLMTargetFeatureOutput,
        source_fingerprint: str,
    ) -> TargetProfileExtractionResult:
        feature_evidence: dict[str, list[Evidence]] = {}
        feature_labels: dict[str, FeatureLabel] = {}

        _add_direct_identity_evidence(ingestion, source_documents, feature_evidence)
        size_metrics = _size_metrics(source_documents)
        if size_metrics:
            _add_size_metric_evidence(source_documents, feature_evidence)

        llm_field_values: dict[str, Any] = {
            "business_summary": llm_output.business_summary,
            "products": llm_output.products,
            "customer_segments": llm_output.customer_segments,
            "channels": llm_output.channels,
            "geographies": llm_output.geographies,
            "keywords": llm_output.keywords,
            "adjacent_categories": llm_output.adjacent_categories,
        }
        for field_name, value in llm_field_values.items():
            if not value:
                continue
            mapped_evidence = _evidence_from_support(field_name, llm_output.field_support.get(field_name, []), source_documents)
            if mapped_evidence:
                feature_evidence[field_name] = mapped_evidence

        for field_name in ("target_id", "name", "ticker", "cik", "exchange", "sic"):
            if field_name in feature_evidence:
                feature_labels[field_name] = FeatureLabel.verified_fact
        if size_metrics:
            feature_labels["size_metrics"] = FeatureLabel.verified_fact

        for field_name in ("business_summary", "products", "customer_segments", "channels", "geographies"):
            if not llm_field_values[field_name]:
                continue
            feature_labels[field_name] = FeatureLabel.verified_fact if feature_evidence.get(field_name) else FeatureLabel.llm_inference
        if llm_output.keywords:
            feature_labels["keywords"] = FeatureLabel.derived_keyword
        if llm_output.adjacent_categories:
            feature_labels["adjacent_categories"] = FeatureLabel.llm_inference

        evidence = _flatten_evidence(feature_evidence)
        profile = TargetProfile(
            target_id=ingestion.target.cik,
            name=ingestion.target.canonical_name,
            ticker=ingestion.target.ticker,
            cik=ingestion.target.cik,
            exchange=ingestion.target.exchange,
            sic=ingestion.target.sic,
            business_summary=llm_output.business_summary,
            products=_dedupe_strings(llm_output.products),
            customer_segments=_dedupe_strings(llm_output.customer_segments),
            channels=_dedupe_strings(llm_output.channels),
            geographies=_dedupe_strings(llm_output.geographies),
            size_metrics=size_metrics,
            keywords=_dedupe_strings(llm_output.keywords),
            adjacent_categories=_dedupe_strings(llm_output.adjacent_categories),
            feature_labels=feature_labels,
            feature_evidence=feature_evidence,
            evidence=evidence,
        )

        return TargetProfileExtractionResult(
            target_profile=profile,
            source_documents=source_documents,
            warnings=warnings,
            extraction_metadata={
                "extractor_version": self.settings.target_profile_extractor_version,
                "source_fingerprint": source_fingerprint,
                "llm_provider": self.settings.llm_provider,
                "llm_model": self.settings.llm_model,
                "cache_hit": False,
                "source_document_count": len(source_documents),
                "source_text_document_count": len([document for document in source_documents if document.raw_text]),
            },
        )


def fingerprint_source_documents(source_documents: list[SourceDocument]) -> str:
    payload = []
    for document in source_documents:
        raw_text_hash = hashlib.sha256((document.raw_text or "").encode("utf-8")).hexdigest() if document.raw_text else None
        payload.append(
            {
                "source_id": document.source_id,
                "source_dimension": document.source_dimension,
                "source_type": document.source_type.value,
                "source_strength": document.source_strength.value,
                "url": document.url,
                "filing_accession": document.filing_accession,
                "metadata": document.metadata,
                "raw_text_hash": raw_text_hash,
            }
        )
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _preferred_text_filing(filings: list[FilingMetadata]) -> FilingMetadata | None:
    return next((filing for filing in filings if filing.form == "10-K"), None) or next(
        (filing for filing in filings if filing.form == "10-Q"),
        None,
    )


def _homepage_url_from_polygon(source_documents: list[SourceDocument]) -> str | None:
    polygon_document = next((document for document in source_documents if document.source_id == "polygon"), None)
    if not polygon_document:
        return None
    for key in ("homepage_url", "homepage", "website_url"):
        value = polygon_document.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_extraction_prompt(
    ingestion: TargetIngestionResult,
    source_documents: list[SourceDocument],
    max_input_chars: int,
) -> str:
    source_blocks: list[str] = []
    remaining_chars = max_input_chars
    for index, document in enumerate(source_documents, start=1):
        text = document.raw_text or json.dumps(document.metadata, sort_keys=True, default=str)
        if not text:
            continue
        excerpt = text[: max(0, remaining_chars)]
        if not excerpt:
            break
        source_blocks.append(
            "\n".join(
                [
                    f"[SOURCE {index}]",
                    f"source_id={document.source_id}",
                    f"source_dimension={document.source_dimension}",
                    f"source_type={document.source_type.value}",
                    f"source_strength={document.source_strength.value}",
                    f"url={document.url}",
                    f"filing_accession={document.filing_accession}",
                    "text:",
                    excerpt,
                ]
            )
        )
        remaining_chars -= len(excerpt)

    return f"""
Extract a minimum but decision-driving target profile for buyer long-list retrieval.

Target identity:
- name: {ingestion.target.canonical_name}
- ticker: {ingestion.target.ticker}
- cik: {ingestion.target.cik}
- exchange: {ingestion.target.exchange}
- sic: {ingestion.target.sic}

Return valid JSON with exactly these top-level keys, and do not omit any key:
business_summary: string or null
products: string[]
customer_segments: string[]
channels: string[]
geographies: string[]
keywords: string[]
adjacent_categories: string[]
field_support: object with the same field names above mapped to short exact source snippets.

Rules:
- Use only the provided sources.
- Keep values concise and useful for retrieval.
- field_support snippets must be copied from the source text when possible.
- Use [] in field_support when a field is empty or has no exact supporting snippet.
- adjacent_categories may be inference, but keep them grounded in source text.

Sources:
{chr(10).join(source_blocks)}
""".strip()


def _add_direct_identity_evidence(
    ingestion: TargetIngestionResult,
    source_documents: list[SourceDocument],
    feature_evidence: dict[str, list[Evidence]],
) -> None:
    mapping_document = next((document for document in source_documents if document.source_type == SourceType.sec_company_mapping), None)
    if not mapping_document:
        return
    evidence = Evidence(
        claim=f"{ingestion.target.canonical_name} resolved to ticker {ingestion.target.ticker} and CIK {ingestion.target.cik}.",
        source_type=mapping_document.source_type.value,
        source_dimension=mapping_document.source_dimension,
        source_strength=mapping_document.source_strength,
        url=mapping_document.url,
        filing_accession=mapping_document.filing_accession,
        retrieved_at=mapping_document.retrieved_at,
        quote_or_snippet=f"{ingestion.target.canonical_name} / {ingestion.target.ticker} / {ingestion.target.cik}",
        verified_fact=True,
    )
    identity_values = {
        "target_id": ingestion.target.cik,
        "name": ingestion.target.canonical_name,
        "ticker": ingestion.target.ticker,
        "cik": ingestion.target.cik,
        "exchange": ingestion.target.exchange,
        "sic": ingestion.target.sic,
    }
    for field_name, value in identity_values.items():
        if value:
            feature_evidence[field_name] = [evidence]


def _size_metrics(source_documents: list[SourceDocument]) -> dict[str, Any]:
    polygon_document = next((document for document in source_documents if document.source_id == "polygon"), None)
    if not polygon_document:
        return {}
    metric_keys = [
        "market_cap",
        "weighted_shares_outstanding",
        "share_class_shares_outstanding",
        "total_employees",
        "employee_count",
    ]
    return {
        key: polygon_document.metadata[key]
        for key in metric_keys
        if isinstance(polygon_document.metadata.get(key), (str, int, float))
    }


def _add_size_metric_evidence(source_documents: list[SourceDocument], feature_evidence: dict[str, list[Evidence]]) -> None:
    polygon_document = next((document for document in source_documents if document.source_id == "polygon"), None)
    if not polygon_document:
        return
    feature_evidence["size_metrics"] = [
        Evidence(
            claim="Polygon profile provides target size metrics used for downstream size-fit filtering.",
            source_type=polygon_document.source_type.value,
            source_dimension=polygon_document.source_dimension,
            source_strength=polygon_document.source_strength,
            url=polygon_document.url,
            filing_accession=polygon_document.filing_accession,
            retrieved_at=polygon_document.retrieved_at,
            quote_or_snippet=json.dumps(_size_metrics(source_documents), sort_keys=True),
            verified_fact=True,
        )
    ]


def _evidence_from_support(
    field_name: str,
    snippets: list[str],
    source_documents: list[SourceDocument],
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for snippet in snippets:
        document = _document_containing_snippet(source_documents, snippet)
        if not document:
            continue
        evidence.append(
            Evidence(
                claim=f"{field_name} is supported by source text.",
                source_type=document.source_type.value,
                source_dimension=document.source_dimension,
                source_strength=document.source_strength,
                url=document.url,
                filing_accession=document.filing_accession,
                retrieved_at=document.retrieved_at,
                quote_or_snippet=snippet,
                verified_fact=True,
            )
        )
    return evidence


def _document_containing_snippet(source_documents: list[SourceDocument], snippet: str) -> SourceDocument | None:
    normalized_snippet = " ".join(snippet.split()).casefold()
    if len(normalized_snippet) < 8:
        return None
    for document in source_documents:
        haystacks = []
        if document.raw_text:
            haystacks.append(" ".join(document.raw_text.split()).casefold())
        if document.metadata:
            haystacks.append(json.dumps(document.metadata, sort_keys=True, default=str).casefold())
        if any(normalized_snippet in haystack for haystack in haystacks):
            return document
    return None


def _flatten_evidence(feature_evidence: dict[str, list[Evidence]]) -> list[Evidence]:
    seen: set[str] = set()
    flattened: list[Evidence] = []
    for evidence_items in feature_evidence.values():
        for evidence in evidence_items:
            key = evidence.model_dump_json()
            if key not in seen:
                flattened.append(evidence)
                seen.add(key)
    return flattened


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        if not normalized or normalized.casefold() in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized.casefold())
    return deduped


def _populated_llm_fields(output: LLMTargetFeatureOutput) -> list[str]:
    field_values: dict[str, Any] = {
        "business_summary": output.business_summary,
        "products": output.products,
        "customer_segments": output.customer_segments,
        "channels": output.channels,
        "geographies": output.geographies,
        "keywords": output.keywords,
        "adjacent_categories": output.adjacent_categories,
    }
    return [field_name for field_name, value in field_values.items() if value]


def _preview_validation_errors(error: ValidationError, limit: int = 800) -> str:
    errors = [
        {
            "loc": item.get("loc"),
            "type": item.get("type"),
            "msg": item.get("msg"),
            "input_type": type(item.get("input")).__name__,
        }
        for item in error.errors()
    ]
    preview = json.dumps(errors, ensure_ascii=False, default=str)
    if len(preview) <= limit:
        return preview
    return f"{preview[:limit]}..."
