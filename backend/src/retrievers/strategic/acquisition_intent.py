"""LLM web-search strategic acquisition-intent retriever."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from src.domain import BuyerType, CandidateHit, Evidence, SourceStrength, StrategicRetrievalResult, TargetProfile
from src.llm import LLMResponseError, MissingLLMConfigurationError, WebSearchJSONClient
from src.retrievers.strategic.common import (
    calendar_years_before,
    first_text,
    is_self_candidate,
    normalize_cik,
    normalize_entity_key,
    normalize_sic,
    required_positive_int,
    required_source_role,
    retriever_config,
    selected_source,
    source_execution_order,
    unique_terms,
)
from src.sources.strategy import DataSourceStrategy


_STRATEGIC_INTENT_SCHEMA_NAME = "StrategicAcquisitionIntentWebSearch"
_STRATEGIC_INTENT_BUSINESS_TYPE = "strategic_buyer_acquisition_intent_web_search"
_WEB_SEARCH_SOURCE_PATH = "strategic_acquisition_intent_llm_web_search"


class _StrategicIntentEvidence(BaseModel):
    """One traceable strategic-intent evidence item returned by provider-managed web search."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    evidence_summary: str = Field(
        default="",
        validation_alias=AliasChoices("evidence_summary", "evidence", "summary", "description", "rationale"),
    )
    source_url: str | None = Field(default=None, validation_alias=AliasChoices("source_url", "url", "link", "source_link"))
    source_title: str | None = Field(default=None, validation_alias=AliasChoices("source_title", "title", "headline"))
    published_date: str | None = Field(
        default=None,
        validation_alias=AliasChoices("published_date", "published_at", "date", "filing_date"),
    )
    quote_or_snippet: str | None = Field(default=None, validation_alias=AliasChoices("quote_or_snippet", "quote", "snippet"))
    intent_type: str | None = Field(default=None, validation_alias=AliasChoices("intent_type", "signal_type", "strategic_signal"))
    sector_relevance: str | None = Field(default=None, validation_alias=AliasChoices("sector_relevance", "relevance"))
    is_relevant: bool = Field(default=True, validation_alias=AliasChoices("is_relevant", "relevant", "has_strategic_intent"))


class _StrategicIntentCandidate(BaseModel):
    """Potential strategic buyer and the source-backed intent signals supporting it."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "candidate_name", "buyer_name", "name", "company"),
    )
    ticker: str | None = Field(default=None, validation_alias=AliasChoices("ticker", "candidate_ticker", "symbol"))
    cik: str | None = Field(default=None, validation_alias=AliasChoices("cik", "candidate_cik"))
    domain: str | None = Field(default=None, validation_alias=AliasChoices("domain", "candidate_domain", "website", "homepage_url"))
    fit_reason: str | None = Field(default=None, validation_alias=AliasChoices("fit_reason", "rationale", "strategic_fit"))
    sector_relevance: str | None = Field(default=None, validation_alias=AliasChoices("sector_relevance", "relevance"))
    has_strategic_intent: bool = Field(
        default=True,
        validation_alias=AliasChoices("has_strategic_intent", "is_relevant", "relevant"),
    )
    evidence: list[_StrategicIntentEvidence] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence", "evidence_items", "sources", "source_evidence", "intent_evidence"),
    )


class _StrategicIntentOutput(BaseModel):
    """Top-level LLM web-search output for strategic acquisition-intent recall."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    target_company: str | None = None
    target_sic: str | None = None
    candidates: list[_StrategicIntentCandidate] = Field(
        default_factory=list,
        validation_alias=AliasChoices("candidates", "potential_buyers", "buyers", "companies", "results"),
    )


@dataclass(frozen=True)
class _StrategicIntentRecord:
    candidate: _StrategicIntentCandidate
    candidate_name: str
    sector_relevance: str
    evidence_items: list[_StrategicIntentEvidence]


class StrategicAcquisitionIntentRetriever:
    """Recalls strategic buyers with sourced acquisition appetite in same or adjacent industries."""

    name = "StrategicAcquisitionIntentRetriever"
    source_path = _WEB_SEARCH_SOURCE_PATH

    def __init__(
        self,
        strategy: DataSourceStrategy,
        *,
        web_search_client: WebSearchJSONClient | None = None,
        as_of_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.web_search_client = web_search_client
        self.as_of_date = as_of_date or date.today()

    def retrieve(self, target_profile: TargetProfile) -> list[CandidateHit]:
        return self.retrieve_with_context(target_profile).hits

    def retrieve_with_context(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        warnings: list[str] = []
        config = retriever_config(self.strategy, self.name, warnings)
        if not config:
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        lookback_years = required_positive_int(config, "lookback_years", self.name, warnings)
        max_candidates = required_positive_int(config, "max_candidates", self.name, warnings)
        max_documents = required_positive_int(config, "max_documents", self.name, warnings)
        max_queries = required_positive_int(config, "max_queries", self.name, warnings)
        max_evidence_per_candidate = required_positive_int(config, "max_evidence_per_candidate", self.name, warnings)
        web_search_source_id = required_source_role(config, "llm_web_search_source", self.name, warnings)
        eligible_sector_matches = set(config.eligible_sector_matches)
        if not eligible_sector_matches:
            warnings.append(f"{self.name} skipped: retriever policy has no eligible_sector_matches")
        if not config.transaction_terms:
            warnings.append(f"{self.name} skipped: retriever policy has no transaction_terms")
        if (
            lookback_years is None
            or max_candidates is None
            or max_documents is None
            or max_queries is None
            or max_evidence_per_candidate is None
            or not web_search_source_id
            or not eligible_sector_matches
            or not config.transaction_terms
        ):
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        web_search_source = selected_source(self.strategy, config.use_case, web_search_source_id, self.name, warnings, required=True)
        metadata = {
            "retriever": self.name,
            "source_use_case": config.use_case,
            "source_roles": config.source_roles,
            "source_priority": source_execution_order(config),
            "source": web_search_source_id,
            "max_candidates": max_candidates,
            "max_documents": max_documents,
            "max_evidence_per_candidate": max_evidence_per_candidate,
        }
        if not web_search_source:
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)
        if not self.web_search_client:
            warnings.append(f"{self.name} {web_search_source_id} unavailable: adapter unavailable")
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)

        since = calendar_years_before(self.as_of_date, lookback_years)
        same_terms, adjacent_terms = _industry_terms(target_profile, max_queries, self.strategy.settings.keyword_taxonomy_path)
        target_sic = normalize_sic(target_profile.sic)
        prompt = _strategic_intent_web_search_prompt(
            target_profile,
            same_terms,
            adjacent_terms,
            config.transaction_terms,
            since,
            self.as_of_date,
            max_candidates=max_candidates,
            max_evidence_per_candidate=max_evidence_per_candidate,
        )
        try:
            payload = self.web_search_client.generate_json_with_web_search(
                prompt,
                _STRATEGIC_INTENT_SCHEMA_NAME,
                _strategic_intent_json_schema(max_candidates, max_evidence_per_candidate),
                system_prompt=_strategic_intent_web_search_system_prompt(),
                max_results=web_search_source.config.retrieval.web_search_max_results,
                max_total_results=web_search_source.config.retrieval.web_search_max_total_results,
                search_engine=web_search_source.config.retrieval.web_search_engine,
                search_context_size=web_search_source.config.retrieval.web_search_context_size,
                fetch_engine=web_search_source.config.retrieval.web_fetch_engine,
                fetch_max_uses=web_search_source.config.retrieval.web_fetch_max_uses,
                fetch_max_content_tokens=web_search_source.config.retrieval.web_fetch_max_content_tokens,
                source_business_type=_STRATEGIC_INTENT_BUSINESS_TYPE,
            )
            output = _StrategicIntentOutput.model_validate(_normalize_web_search_payload(payload))
        except MissingLLMConfigurationError as error:
            warnings.append(f"{self.name} LLM web search unavailable: {error}")
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)
        except (LLMResponseError, ValidationError, ValueError) as error:
            warnings.append(f"{self.name} LLM web search failed: {type(error).__name__}: {error}")
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)

        records, skip_reasons = _strategic_intent_records(output, target_profile, eligible_sector_matches)
        evidence_available_before_cap = sum(len(record.evidence_items) for record in records)
        hits: list[CandidateHit] = []
        evidence_used = 0
        evidence_budget = max_documents
        for record in records:
            if len(hits) >= max_candidates or evidence_budget <= 0:
                break
            selected_items = record.evidence_items[: min(max_evidence_per_candidate, evidence_budget)]
            hit = _hit_from_record(
                record,
                selected_items,
                target_profile,
                retriever_name=self.name,
                source_id=web_search_source.source_id,
                source_dimension=web_search_source.dimension_id,
                source_strength=web_search_source.source_strength,
            )
            if not hit:
                skip_reasons["missing_evidence"] = skip_reasons.get("missing_evidence", 0) + 1
                continue
            hits.append(hit)
            evidence_used += len(hit.evidence)
            evidence_budget -= len(hit.evidence)

        hits.sort(key=lambda hit: (-hit.confidence, hit.candidate_name.casefold()))
        candidate_records_truncated = max(0, len(records) - len(hits))
        evidence_truncated = max(0, evidence_available_before_cap - evidence_used)
        if candidate_records_truncated:
            warnings.append(f"{self.name} truncated {candidate_records_truncated} strategic-intent candidates above configured caps")
        if evidence_truncated:
            warnings.append(f"{self.name} truncated {evidence_truncated} strategic-intent evidence item(s) above configured caps")

        metadata.update(
            {
                "target_sic": target_sic,
                "lookback_start": since.isoformat(),
                "lookback_end": self.as_of_date.isoformat(),
                "same_industry_terms": same_terms,
                "adjacent_industry_terms": adjacent_terms,
                "intent_terms": config.transaction_terms,
                "llm_web_search_candidates_checked": len(output.candidates),
                "candidate_records_available_before_cap": len(records),
                "candidate_records_truncated": candidate_records_truncated,
                "evidence_available_before_cap": evidence_available_before_cap,
                "evidence_used": evidence_used,
                "evidence_truncated": evidence_truncated,
                "skip_reasons": skip_reasons,
                "hit_count": len(hits),
            }
        )
        return StrategicRetrievalResult(hits=hits, warnings=warnings, metadata=metadata)


def _strategic_intent_records(
    output: _StrategicIntentOutput,
    target_profile: TargetProfile,
    eligible_sector_matches: set[str],
) -> tuple[list[_StrategicIntentRecord], dict[str, int]]:
    records: list[_StrategicIntentRecord] = []
    skip_reasons: dict[str, int] = {}
    seen: set[str] = set()
    for candidate in output.candidates:
        candidate_name = _clean_text(candidate.company_name)
        if not candidate_name:
            _increment(skip_reasons, "missing_name")
            continue
        if not candidate.has_strategic_intent:
            _increment(skip_reasons, "no_strategic_intent")
            continue
        if is_self_candidate(_candidate_identity_metadata(candidate), target_profile, candidate_name):
            _increment(skip_reasons, "self_candidate")
            continue
        candidate_key = normalize_entity_key(candidate_name)
        if candidate_key in seen:
            _increment(skip_reasons, "duplicate_candidate")
            continue
        sector_relevance = _candidate_sector_relevance(candidate)
        if sector_relevance not in eligible_sector_matches:
            _increment(skip_reasons, "unrelated_sector")
            continue

        evidence_items = _valid_evidence_items(candidate.evidence, sector_relevance, eligible_sector_matches)
        if not evidence_items:
            _increment(skip_reasons, "missing_evidence")
            continue
        seen.add(candidate_key)
        records.append(
            _StrategicIntentRecord(
                candidate=candidate,
                candidate_name=candidate_name,
                sector_relevance=sector_relevance,
                evidence_items=evidence_items,
            )
        )
    return records, skip_reasons


def _hit_from_record(
    record: _StrategicIntentRecord,
    evidence_items: list[_StrategicIntentEvidence],
    target_profile: TargetProfile,
    *,
    retriever_name: str,
    source_id: str,
    source_dimension: str | None,
    source_strength: SourceStrength,
) -> CandidateHit | None:
    evidence = [
        _evidence_from_item(item, record.candidate_name, target_profile, source_dimension, source_strength)
        for item in evidence_items
    ]
    evidence = [item for item in evidence if item is not None]
    if not evidence:
        return None

    return CandidateHit(
        candidate_name=record.candidate_name,
        candidate_ticker=_clean_text(record.candidate.ticker),
        candidate_cik=normalize_cik(record.candidate.cik),
        candidate_domain=_clean_domain(record.candidate.domain),
        buyer_type=BuyerType.strategic,
        retriever_name=retriever_name,
        source_path=[_WEB_SEARCH_SOURCE_PATH],
        data_source=[source_id],
        fit_reason=_fit_reason(record, target_profile),
        evidence=evidence,
        confidence=_confidence(record.sector_relevance, len(evidence)),
        pending_verification=True,
        retrieval_metadata={
            "target_sic": normalize_sic(target_profile.sic),
            "sector_relevance": record.sector_relevance,
            "intent_types": unique_terms([item.intent_type or "" for item in evidence_items]),
            "llm_web_search_evidence_count": len(evidence),
            "pending_reason": "llm_web_search_claims_need_source_page_validation",
        },
    )


def _evidence_from_item(
    item: _StrategicIntentEvidence,
    candidate_name: str,
    target_profile: TargetProfile,
    source_dimension: str | None,
    source_strength: SourceStrength,
) -> Evidence | None:
    source_url = _clean_text(item.source_url)
    evidence_text = _evidence_text(item)
    if not source_url or not evidence_text:
        return None
    return Evidence(
        claim=f"{candidate_name} has sourced strategic acquisition intent relevant to {target_profile.name}'s industry context.",
        source_type="llm_web_search",
        source_dimension=source_dimension,
        source_strength=source_strength,
        url=source_url,
        quote_or_snippet=_snippet(evidence_text),
        verified_fact=False,
    )


def _valid_evidence_items(
    evidence_items: list[_StrategicIntentEvidence],
    candidate_relevance: str,
    eligible_sector_matches: set[str],
) -> list[_StrategicIntentEvidence]:
    valid_items: list[_StrategicIntentEvidence] = []
    seen_urls: set[str] = set()
    for item in evidence_items:
        if not item.is_relevant:
            continue
        source_url = _clean_text(item.source_url)
        if not source_url or source_url in seen_urls:
            continue
        relevance = _normalize_sector_relevance(item.sector_relevance) or candidate_relevance
        if relevance not in eligible_sector_matches:
            continue
        if not _evidence_text(item):
            continue
        seen_urls.add(source_url)
        valid_items.append(item)
    return valid_items


def _candidate_sector_relevance(candidate: _StrategicIntentCandidate) -> str:
    relevance = _normalize_sector_relevance(candidate.sector_relevance)
    if relevance:
        return relevance
    relevance = _normalize_sector_relevance(candidate.fit_reason)
    if relevance:
        return relevance
    for item in candidate.evidence:
        relevance = _normalize_sector_relevance(item.sector_relevance)
        if relevance:
            return relevance
        for text in (item.evidence_summary, item.quote_or_snippet):
            relevance = _normalize_sector_relevance(text)
            if relevance:
                return relevance
    return "unrelated"


def _candidate_identity_metadata(candidate: _StrategicIntentCandidate) -> dict[str, Any]:
    return {
        "ticker": candidate.ticker,
        "candidate_ticker": candidate.ticker,
        "cik": candidate.cik,
        "candidate_cik": candidate.cik,
    }


def _fit_reason(record: _StrategicIntentRecord, target_profile: TargetProfile) -> str:
    fit_reason = _clean_text(record.candidate.fit_reason)
    if fit_reason:
        return fit_reason
    relevance_label = "same-industry" if record.sector_relevance == "same" else "adjacent-industry"
    return f"Shows sourced strategic acquisition intent in {relevance_label} context for {target_profile.name}."


def _confidence(sector_relevance: str, evidence_count: int) -> float:
    base = 0.54 if sector_relevance == "same" else 0.48
    return round(min(0.64, base + max(0, evidence_count - 1) * 0.03), 2)


def _normalize_web_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept common LLM field variants while still validating the canonical schema locally."""

    normalized = dict(payload)
    if "target_sic" in normalized and normalized["target_sic"] is not None:
        normalized["target_sic"] = normalize_sic(normalized["target_sic"])
    if "candidates" not in normalized:
        for key in ("potential_buyers", "buyers", "companies", "results"):
            value = normalized.get(key)
            if isinstance(value, list):
                normalized["candidates"] = value
                break
    candidates = normalized.get("candidates")
    if isinstance(candidates, dict):
        normalized["candidates"] = [candidates]
    if isinstance(candidates, list):
        normalized["candidates"] = [_normalize_candidate(candidate) for candidate in candidates if isinstance(candidate, dict)]
    return normalized


def _normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    # Some provider-managed web-search runs return null for required boolean flags.
    # Treat null like an omitted flag so a single malformed field does not discard
    # otherwise source-backed candidates before evidence filtering can run.
    for key in ("has_strategic_intent", "is_relevant", "relevant"):
        if key in normalized and normalized[key] is None:
            normalized[key] = True
    evidence = None
    for key in ("evidence", "evidence_items", "sources", "source_evidence", "intent_evidence"):
        if key in normalized:
            evidence = normalized[key]
            break
    if evidence is None:
        source_url, source_title = _first_source_reference(normalized)
        if source_url:
            evidence = [
                {
                    "source_url": source_url,
                    "source_title": source_title,
                    "evidence_summary": first_text(normalized, "evidence_summary", "summary", "rationale", "fit_reason") or "",
                    "sector_relevance": first_text(normalized, "sector_relevance", "relevance"),
                }
            ]
    normalized["evidence"] = _normalize_evidence_list(evidence)
    return normalized


def _normalize_evidence_list(raw_evidence: Any) -> list[dict[str, Any]]:
    if raw_evidence is None:
        return []
    if isinstance(raw_evidence, dict):
        raw_items = [raw_evidence]
    elif isinstance(raw_evidence, list):
        raw_items = raw_evidence
    elif isinstance(raw_evidence, str):
        raw_items = [{"evidence_summary": raw_evidence}]
    else:
        raw_items = []
    return [_normalize_evidence_item(item) for item in raw_items if isinstance(item, (dict, str))]


def _normalize_evidence_item(raw_item: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw_item, str):
        return {"evidence_summary": raw_item}
    normalized = dict(raw_item)
    for key in ("is_relevant", "relevant", "has_strategic_intent"):
        if key in normalized and normalized[key] is None:
            normalized[key] = True
    if not first_text(normalized, "source_url", "url", "link", "source_link"):
        source_url, source_title = _first_source_reference(normalized)
        if source_url:
            normalized["source_url"] = source_url
        if source_title and not first_text(normalized, "source_title", "title", "headline"):
            normalized["source_title"] = source_title
    if not first_text(normalized, "evidence_summary", "evidence", "summary", "description", "rationale"):
        fallback = first_text(normalized, "quote_or_snippet", "quote", "snippet", "source_title", "title", "headline")
        if fallback:
            normalized["evidence_summary"] = fallback
    if "sector_relevance" in normalized or "relevance" in normalized:
        normalized["sector_relevance"] = _normalize_sector_relevance(first_text(normalized, "sector_relevance", "relevance"))
    return normalized


def _first_source_reference(value: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("source_links", "source_urls", "links", "urls", "references"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip(), None
            if isinstance(item, dict):
                url = first_text(item, "source_url", "url", "link", "href")
                title = first_text(item, "source_title", "title", "name", "headline")
                if url:
                    return url, title
    source = value.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip(), None
    if isinstance(source, dict):
        return first_text(source, "source_url", "url", "link", "href"), first_text(source, "source_title", "title", "name")
    return None, None


def _normalize_sector_relevance(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z]+", " ", value.casefold()).strip()
    if not normalized:
        return None
    if re.search(r"\bunrelated\b|\bnot related\b|\bno relevance\b|\boff topic\b", normalized):
        return "unrelated"
    if re.search(r"\badjacent\b|\brelated\b|\bnearby\b|\bneighboring\b|\bchannel\b|\bcustomer\b", normalized):
        return "adjacent"
    if re.search(r"\bsame\b|\bdirect\b|\bcore\b|\bcategory\b|\bindustry\b", normalized):
        return "same"
    return None


def _strategic_intent_web_search_prompt(
    target_profile: TargetProfile,
    same_terms: list[str],
    adjacent_terms: list[str],
    intent_terms: list[str],
    since: date,
    as_of_date: date,
    *,
    max_candidates: int,
    max_evidence_per_candidate: int,
) -> str:
    sic = normalize_sic(target_profile.sic) or "unknown"
    adjacent_text = ", ".join(adjacent_terms[:12]) or "not available"
    intent_text = ", ".join(intent_terms[:12]) or "acquisition, M&A, corporate development"
    return (
        "Use web search. "
        "Find operating companies, not private equity or other financial sponsors, that have sourced strategic acquisition "
        f"intent in the same or adjacent industries from {since.isoformat()} through {as_of_date.isoformat()}. "
        f"Industry context only: SIC={sic}. "
        f"Adjacent-industry terms: {adjacent_text}. "
        f"Strategic intent terms: {intent_text}. "
        "Prioritize SEC filings, investor-relations press releases, official acquisition pages, and official investor presentations. "
        "Do not search by or mention a specific seller company name, and do not infer a seller-specific transaction rumor. "
        "Accept evidence such as official strategy pages, investor presentations, earnings-call statements, corporate-development "
        "hiring pages, press releases, or reputable news saying the company plans, seeks, prioritizes, or actively pursues "
        "acquisitions or M&A in the relevant category. "
        f"Return at most {max_candidates} companies and at most {max_evidence_per_candidate} evidence items per company. "
        "Every evidence item must include a source URL."
    )


def _strategic_intent_web_search_system_prompt() -> str:
    return (
        f"Return only valid JSON for schema {_STRATEGIC_INTENT_SCHEMA_NAME}. "
        "The JSON object must contain target_sic and candidates. Each candidate must include company_name, "
        "ticker, cik, domain, sector_relevance, fit_reason, has_strategic_intent, and evidence. Each evidence item must include "
        "evidence_summary, source_url, source_title, published_date, quote_or_snippet, intent_type, sector_relevance, and "
        "is_relevant. For every sector_relevance field, use exactly same, adjacent, or unrelated; do not use High, Medium, "
        "Low, or numeric relevance scores. Use null for unknown optional values and an empty candidates array when no sourced "
        "relevant buyer is found."
    )


def _strategic_intent_json_schema(max_candidates: int, max_evidence_per_candidate: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["target_sic", "candidates"],
        "properties": {
            "target_sic": {"type": ["string", "null"]},
            "candidates": {
                "type": "array",
                "maxItems": max_candidates,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "company_name",
                        "ticker",
                        "cik",
                        "domain",
                        "sector_relevance",
                        "fit_reason",
                        "has_strategic_intent",
                        "evidence",
                    ],
                    "properties": {
                        "company_name": {"type": "string"},
                        "ticker": {"type": ["string", "null"]},
                        "cik": {"type": ["string", "null"]},
                        "domain": {"type": ["string", "null"]},
                        "sector_relevance": {
                            "type": "string",
                            "description": "Industry relevance. Use exactly same, adjacent, or unrelated.",
                        },
                        "fit_reason": {"type": "string"},
                        "has_strategic_intent": {"type": "boolean"},
                        "evidence": {
                            "type": "array",
                            "maxItems": max_evidence_per_candidate,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "evidence_summary",
                                    "source_url",
                                    "source_title",
                                    "published_date",
                                    "quote_or_snippet",
                                    "intent_type",
                                    "sector_relevance",
                                    "is_relevant",
                                ],
                                "properties": {
                                    "evidence_summary": {"type": "string"},
                                    "source_url": {"type": "string"},
                                    "source_title": {"type": ["string", "null"]},
                                    "published_date": {"type": ["string", "null"]},
                                    "quote_or_snippet": {"type": ["string", "null"]},
                                    "intent_type": {"type": ["string", "null"]},
                                    "sector_relevance": {
                                        "type": "string",
                                        "description": "Industry relevance. Use exactly same, adjacent, or unrelated.",
                                    },
                                    "is_relevant": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def _industry_terms(target_profile: TargetProfile, max_queries: int, taxonomy_path: Any) -> tuple[list[str], list[str]]:
    taxonomy_terms = _sic_taxonomy_terms(taxonomy_path, normalize_sic(target_profile.sic))
    same_terms = unique_terms([*taxonomy_terms, *target_profile.products, *target_profile.keywords])[:max_queries]
    adjacent_terms = unique_terms(
        [*target_profile.adjacent_categories, *target_profile.customer_segments, *target_profile.channels]
    )[:max_queries]
    return same_terms, adjacent_terms


def _sic_taxonomy_terms(taxonomy_path: Any, sic: str | None) -> list[str]:
    if not taxonomy_path or not sic:
        return []
    path = Path(taxonomy_path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as taxonomy_file:
        raw_taxonomy = yaml.safe_load(taxonomy_file) or {}
    raw_sic_keywords = raw_taxonomy.get("sic_keywords", {})
    if not isinstance(raw_sic_keywords, dict):
        return []
    entry = raw_sic_keywords.get(str(sic))
    if not isinstance(entry, dict):
        return []
    label = entry.get("label")
    keywords = entry.get("keywords", [])
    keyword_terms = [str(keyword) for keyword in keywords] if isinstance(keywords, list) else []
    return unique_terms([str(label) if label else "", *keyword_terms])


def _evidence_text(item: _StrategicIntentEvidence) -> str | None:
    return _clean_text(item.quote_or_snippet) or _clean_text(item.evidence_summary) or _clean_text(item.source_title)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_domain(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return re.sub(r"^https?://", "", text, flags=re.IGNORECASE).strip("/").lower()


def _snippet(text: str, max_chars: int = 320) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
