"""FMP-backed PE deal activity retriever."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from src.domain import (
    BuyerType,
    CandidateHit,
    DealEvent,
    Evidence,
    SourceDocument,
    SourceStrength,
    SourceType,
    StrategicRetrievalResult,
    TargetProfile,
)
from src.llm import LLMResponseError, MissingLLMConfigurationError, WebSearchJSONClient
from src.retrievers.financial.protocols import PEDealActivitySource
from src.retrievers.financial.seed_universe import PESeedMatcher, PESeedMatch, PESeedUniverse, load_pe_seed_universe
from src.retrievers.strategic.common import (
    calendar_years_before,
    first_text,
    normalize_entity_key,
    normalize_sic,
    parse_date,
    required_positive_int,
    required_source_role,
    retriever_config,
    selected_source,
    source_execution_order,
    source_type_value,
    unique_terms,
)
from src.sources.strategy import DataSourceStrategy


@dataclass(frozen=True)
class _PEDealSignal:
    event: DealEvent
    evidence: Evidence
    document: SourceDocument
    seed_match: PESeedMatch
    raw_acquirer: str
    query_term: str | None
    source_path: str


class _PEDealWebSearchDeal(BaseModel):
    """Structured PE deal signal returned by provider-managed web search."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    has_relevant_deal: bool = Field(default=True, validation_alias=AliasChoices("has_relevant_deal", "is_relevant", "relevant"))
    evidence_summary: str = Field(
        default="",
        min_length=0,
        validation_alias=AliasChoices("evidence_summary", "evidence", "evidence_description", "description", "summary"),
    )
    acquisition_date: str | None = Field(
        default=None,
        validation_alias=AliasChoices("acquisition_date", "acquired_date", "deal_date", "transaction_date", "date", "acquisition_time"),
    )
    acquired_company: str | None = Field(
        default=None,
        validation_alias=AliasChoices("acquired_company", "acquired_target", "target_company", "target", "company_acquired"),
    )
    source_url: str | None = Field(default=None, validation_alias=AliasChoices("source_url", "url", "link", "source_link"))
    source_title: str | None = Field(default=None, validation_alias=AliasChoices("source_title", "title", "headline"))
    sector_relevance: str = Field(default="same", validation_alias=AliasChoices("sector_relevance", "relevance"))
    deal_type: str | None = Field(default=None, validation_alias=AliasChoices("deal_type", "transaction_type", "type"))


class _PEDealWebSearchOutput(BaseModel):
    """Top-level LLM web-search output for one configured PE firm and one target SIC."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    pe_firm: str | None = None
    sic: str | None = None
    deals: list[_PEDealWebSearchDeal] = Field(default_factory=list)


_PE_DEAL_WEB_SEARCH_SCHEMA_NAME = "PEDealActivityWebSearch"
_PE_DEAL_WEB_SEARCH_BUSINESS_TYPE = "financial_buyer_pe_deal_activity_web_search"
_LLM_WEB_SEARCH_SOURCE_ID = "llm_web_search"
_WEB_SEARCH_SOURCE_PATH = "pe_deal_activity_llm_web_search"
_PE_DEAL_WEB_SEARCH_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pe_firm", "sic", "deals"],
    "properties": {
        "pe_firm": {"type": ["string", "null"]},
        "sic": {"type": ["string", "null"]},
        "deals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "has_relevant_deal",
                    "evidence_summary",
                    "acquisition_date",
                    "acquired_company",
                    "source_url",
                    "source_title",
                    "sector_relevance",
                    "deal_type",
                ],
                "properties": {
                    "has_relevant_deal": {"type": "boolean"},
                    "evidence_summary": {"type": "string"},
                    "acquisition_date": {"type": ["string", "null"]},
                    "acquired_company": {"type": ["string", "null"]},
                    "source_url": {"type": ["string", "null"]},
                    "source_title": {"type": ["string", "null"]},
                    "sector_relevance": {
                        "type": "string",
                        "description": (
                            "Industry relevance. Prefer same, adjacent, or unrelated, but short explanatory values "
                            "such as 'Same-industry: cosmetics' are accepted."
                        ),
                    },
                    "deal_type": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class PEDealActivityRetriever:
    """Recalls financial buyers with recent same/adjacent-industry FMP deal signals."""

    name = "PEDealActivityRetriever"
    source_path = "pe_deal_activity"

    def __init__(
        self,
        strategy: DataSourceStrategy,
        *,
        fmp_client: PEDealActivitySource | None = None,
        web_search_client: WebSearchJSONClient | None = None,
        seed_universe: PESeedUniverse | None = None,
        as_of_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.fmp_client = fmp_client
        self.web_search_client = web_search_client
        self.seed_universe = seed_universe
        self.as_of_date = as_of_date or date.today()

    def retrieve(self, target_profile: TargetProfile) -> list[CandidateHit]:
        return self.retrieve_with_context(target_profile).hits

    def retrieve_with_context(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        warnings: list[str] = []
        config = retriever_config(self.strategy, self.name, warnings)
        if not config:
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        lookback_years = required_positive_int(config, "lookback_years", self.name, warnings)
        max_documents = required_positive_int(config, "max_documents", self.name, warnings)
        max_queries = required_positive_int(config, "max_queries", self.name, warnings)
        page_size = required_positive_int(config, "page_size", self.name, warnings)
        max_candidates = required_positive_int(config, "max_candidates", self.name, warnings)
        deal_source_id = required_source_role(config, "deal_activity_source", self.name, warnings)
        llm_web_search_source_id = config.source_roles.get("llm_web_search_source")
        eligible_sector_matches = set(config.eligible_sector_matches)
        if not eligible_sector_matches:
            warnings.append(f"{self.name} skipped: retriever policy has no eligible_sector_matches")
        if not config.transaction_terms:
            warnings.append(f"{self.name} skipped: retriever policy has no transaction_terms")
        if (
            lookback_years is None
            or max_documents is None
            or max_queries is None
            or page_size is None
            or max_candidates is None
            or not deal_source_id
            or not eligible_sector_matches
            or not config.transaction_terms
        ):
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        seed_universe = self._seed_universe(warnings)
        if seed_universe is None or not seed_universe.firms:
            warnings.append(f"{self.name} skipped: PE seed universe is empty or unavailable")
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})
        company_limit = config.max_companies or len(seed_universe.firms)

        matcher = PESeedMatcher(seed_universe)
        since = calendar_years_before(self.as_of_date, lookback_years)
        query_terms, same_terms, adjacent_terms = _industry_query_terms(
            target_profile,
            max_queries,
            self.strategy.settings.keyword_taxonomy_path,
        )
        documents: list[SourceDocument] = []
        fmp_document_count = 0
        llm_document_count = 0
        llm_deal_count = 0
        llm_error_count = 0
        llm_skipped_firm_count = 0

        for source_id in source_execution_order(config):
            if source_id != deal_source_id:
                if source_id == llm_web_search_source_id:
                    llm_source = selected_source(self.strategy, config.use_case, source_id, self.name, warnings, required=False)
                    if not llm_source:
                        continue
                    if not self.web_search_client:
                        warnings.append(f"{self.name} {source_id} unavailable: adapter unavailable")
                        continue
                    for firm in seed_universe.firms[:company_limit]:
                        try:
                            web_documents = _web_search_documents_for_firm(
                                self.web_search_client,
                                llm_source.config.retrieval,
                                firm.canonical_name,
                                target_profile,
                                same_terms,
                                adjacent_terms,
                                since,
                                self.as_of_date,
                                source_id=llm_source.source_id,
                                source_dimension=llm_source.dimension_id,
                                source_strength=llm_source.source_strength,
                            )
                        except MissingLLMConfigurationError as error:
                            warnings.append(f"{self.name} LLM web search unavailable: {error}")
                            break
                        except (LLMResponseError, ValidationError, ValueError) as error:
                            llm_error_count += 1
                            warnings.append(
                                f"{self.name} LLM web search failed for {firm.canonical_name}: {type(error).__name__}: {error}"
                            )
                            continue
                        if not web_documents:
                            llm_skipped_firm_count += 1
                            continue
                        llm_document_count += len(web_documents)
                        llm_deal_count += len(web_documents)
                        documents.extend(web_documents)
                continue

            fmp_source = selected_source(self.strategy, config.use_case, source_id, self.name, warnings, required=True)
            if not fmp_source:
                continue
            if not self.fmp_client:
                warnings.append(f"{self.name} {source_id} unavailable: adapter unavailable")
                continue

            for query in query_terms:
                if len(documents) >= max_documents:
                    break
                page = 0
                while len(documents) < max_documents:
                    try:
                        page_documents = self.fmp_client.search_mergers_acquisitions(
                            query,
                            self.strategy.cache_ttl_hours(source_id),
                            source_strength=fmp_source.source_strength,
                            source_dimension=fmp_source.dimension_id,
                            limit=page_size,
                            page=page,
                        )
                    except Exception as error:
                        warnings.append(f"{self.name} FMP query failed: {type(error).__name__}: {_safe_error_message(error)}")
                        break
                    if not page_documents:
                        break
                    fmp_document_count += len(page_documents)
                    documents.extend(_annotate_query_documents(page_documents, query, self.source_path))
                    if len(page_documents) < page_size:
                        break
                    page += 1

        deduped_documents = _dedupe_documents(documents)
        documents_available_before_cap = len(deduped_documents)
        unique_documents = _cap_documents_preserving_sources(deduped_documents, max_documents)
        documents_truncated = max(0, documents_available_before_cap - len(unique_documents))
        grouped_signals: dict[str, list[_PEDealSignal]] = defaultdict(list)
        skipped_old = 0
        skipped_future = 0
        skipped_without_date = 0
        skipped_without_buyer = 0
        skipped_non_seed = 0
        skipped_unrelated = 0
        skipped_without_transaction_language = 0

        for document in unique_documents:
            signal = _deal_signal_from_document(
                document,
                target_profile,
                matcher,
                config.transaction_terms,
                same_terms,
                adjacent_terms,
            )
            if not signal:
                if not _extract_raw_acquirer(document):
                    skipped_without_buyer += 1
                elif not _matched_seed(document, matcher):
                    skipped_non_seed += 1
                elif not _document_has_transaction_language(document, config.transaction_terms):
                    skipped_without_transaction_language += 1
                else:
                    skipped_unrelated += 1
                continue

            event_date = parse_date(signal.event.deal_date)
            if not event_date:
                skipped_without_date += 1
                continue
            if event_date < since:
                skipped_old += 1
                continue
            if event_date > self.as_of_date:
                skipped_future += 1
                continue
            if signal.event.sector_match not in eligible_sector_matches:
                skipped_unrelated += 1
                continue

            grouped_signals[normalize_entity_key(signal.seed_match.firm.canonical_name)].append(signal)

        hits = [_hit_from_signals(signal_group, self.name, since) for signal_group in grouped_signals.values()]
        hits.sort(key=lambda hit: (-hit.confidence, hit.candidate_name.casefold()))
        if len(hits) > max_candidates:
            warnings.append(f"{self.name} truncated {len(hits) - max_candidates} financial sponsor hits above max_candidates")
            hits = hits[:max_candidates]

        if skipped_old:
            warnings.append(f"{self.name} skipped {skipped_old} FMP deal(s) older than {since.isoformat()}")
        if skipped_future:
            warnings.append(f"{self.name} skipped {skipped_future} FMP deal(s) after {self.as_of_date.isoformat()}")
        if skipped_without_date:
            warnings.append(f"{self.name} skipped {skipped_without_date} FMP deal(s) without a parseable transaction date")
        if skipped_without_buyer:
            warnings.append(f"{self.name} skipped {skipped_without_buyer} FMP deal(s) without a parsed acquirer")
        if skipped_non_seed:
            warnings.append(f"{self.name} skipped {skipped_non_seed} non-seed acquirer deal(s)")
        if skipped_unrelated:
            warnings.append(f"{self.name} skipped {skipped_unrelated} unrelated FMP deal(s)")
        if skipped_without_transaction_language:
            warnings.append(f"{self.name} skipped {skipped_without_transaction_language} FMP record(s) without transaction language")

        return StrategicRetrievalResult(
            hits=hits,
            warnings=warnings,
            metadata={
                "retriever": self.name,
                "source_use_case": config.use_case,
                "source_roles": config.source_roles,
                "source_priority": config.source_priority,
                "source": deal_source_id,
                "lookback_start": since.isoformat(),
                "lookback_end": self.as_of_date.isoformat(),
                "query_terms": query_terms,
                "same_industry_terms": same_terms,
                "adjacent_industry_terms": adjacent_terms,
                "seed_firm_count": len(seed_universe.firms),
                "seed_firms_checked": min(company_limit, len(seed_universe.firms)),
                "fmp_documents_checked": fmp_document_count,
                "llm_web_search_documents_checked": llm_document_count,
                "llm_web_search_deal_count": llm_deal_count,
                "llm_web_search_error_count": llm_error_count,
                "llm_web_search_no_deal_firm_count": llm_skipped_firm_count,
                "documents_available_before_cap": documents_available_before_cap,
                "documents_truncated": documents_truncated,
                "documents_checked": len(unique_documents),
                "skip_reasons": {
                    "older_than_lookback": skipped_old,
                    "future_dated": skipped_future,
                    "missing_date": skipped_without_date,
                    "missing_acquirer": skipped_without_buyer,
                    "non_seed_acquirer": skipped_non_seed,
                    "unrelated_sector": skipped_unrelated,
                    "no_transaction_language": skipped_without_transaction_language,
                },
                "hit_count": len(hits),
            },
        )

    def _seed_universe(self, warnings: list[str]) -> PESeedUniverse | None:
        if self.seed_universe is not None:
            return self.seed_universe
        try:
            return load_pe_seed_universe(Path(self.strategy.settings.pe_seed_universe_path))
        except Exception as error:
            warnings.append(f"{self.name} failed to load PE seed universe: {type(error).__name__}: {error}")
            return None


def _hit_from_signals(signals: list[_PEDealSignal], retriever_name: str, since: date) -> CandidateHit:
    firm = signals[0].seed_match.firm
    events = [signal.event for signal in signals]
    evidence = [signal.evidence for signal in signals]
    same_count = sum(1 for event in events if event.sector_match == "same")
    adjacent_count = sum(1 for event in events if event.sector_match == "adjacent")
    pending_verification = any(_is_llm_web_search_document(signal.document) for signal in signals)
    base_confidence = 0.76 if same_count else 0.66
    confidence = min(0.9, base_confidence + max(0, len(events) - 1) * 0.04)
    if pending_verification and all(_is_llm_web_search_document(signal.document) for signal in signals):
        confidence = min(confidence, 0.58)

    return CandidateHit(
        candidate_name=firm.canonical_name,
        candidate_domain=firm.domain,
        buyer_type=BuyerType.financial,
        retriever_name=retriever_name,
        source_path=unique_terms([signal.source_path for signal in signals]),
        fit_reason=_financial_fit_reason(same_count, adjacent_count, since),
        evidence=evidence,
        confidence=confidence,
        pending_verification=pending_verification,
        retrieval_metadata={
            "lookback_start": since.isoformat(),
            "matched_seed_firm": firm.canonical_name,
            "matched_aliases": unique_terms([signal.seed_match.matched_name for signal in signals]),
            "raw_acquirers": unique_terms([signal.raw_acquirer for signal in signals]),
            "query_terms": unique_terms([signal.query_term or "" for signal in signals]),
            "deal_events": [event.model_dump(mode="json") for event in events],
            "same_sector_event_count": same_count,
            "adjacent_sector_event_count": adjacent_count,
            "llm_web_search_event_count": sum(1 for signal in signals if _is_llm_web_search_document(signal.document)),
        },
    )


def _financial_fit_reason(same_count: int, adjacent_count: int, since: date) -> str:
    total = same_count + adjacent_count
    if same_count and adjacent_count:
        return f"Completed {total} same or adjacent-sector PE deal signal(s) since {since.isoformat()}."
    if same_count:
        return f"Completed {same_count} same-sector PE deal signal(s) since {since.isoformat()}."
    return f"Completed {adjacent_count} adjacent-sector PE deal signal(s) since {since.isoformat()}."


def _deal_signal_from_document(
    document: SourceDocument,
    target_profile: TargetProfile,
    matcher: PESeedMatcher,
    transaction_terms: list[str],
    same_terms: list[str],
    adjacent_terms: list[str],
) -> _PEDealSignal | None:
    raw_acquirer = _extract_raw_acquirer(document)
    if not raw_acquirer:
        return None

    cleaned_acquirer = _clean_acquirer_name(raw_acquirer)
    seed_match = matcher.match(cleaned_acquirer) or matcher.match(raw_acquirer)
    if not seed_match:
        return None

    if not _document_has_transaction_language(document, transaction_terms):
        return None

    text = _document_text(document)
    structured_sector_match = first_text(document.metadata, "sector_relevance")
    sector_match = _normalize_sector_relevance(structured_sector_match) or _sector_match(
        text, same_terms, adjacent_terms, target_profile
    )
    if sector_match == "unrelated":
        return None

    event = DealEvent(
        buyer=seed_match.firm.canonical_name,
        target_acquired=_extract_acquired_target(document),
        deal_date=_document_date(document),
        deal_type=_deal_type(document),
        sector_match=sector_match,
        source_type=source_type_value(document.source_type),
        url=document.url,
        filing_accession=document.filing_accession,
        quote_or_snippet=_snippet(document.raw_text or text),
    )
    return _PEDealSignal(
        event=event,
        evidence=_evidence_from_event(event, document),
        document=document,
        seed_match=seed_match,
        raw_acquirer=raw_acquirer,
        query_term=first_text(document.metadata, "fmp_query"),
        source_path=first_text(document.metadata, "source_path") or "pe_deal_activity",
    )


def _evidence_from_event(event: DealEvent, document: SourceDocument) -> Evidence:
    target_text = f" involving {event.target_acquired}" if event.target_acquired else ""
    discovered_from = first_text(document.metadata, "discovered_from")
    return Evidence(
        claim=(
            f"{event.buyer} had a {event.deal_type or 'deal activity'} signal"
            f"{target_text} with {event.sector_match} target-industry relevance."
        ),
        source_type=source_type_value(document.source_type),
        source_dimension=document.source_dimension,
        source_strength=document.source_strength,
        url=document.url,
        filing_accession=document.filing_accession,
        quote_or_snippet=event.quote_or_snippet,
        verified_fact=discovered_from != "llm_web_search",
    )


def _web_search_documents_for_firm(
    web_search_client: WebSearchJSONClient,
    retrieval_config: Any,
    pe_firm: str,
    target_profile: TargetProfile,
    same_terms: list[str],
    adjacent_terms: list[str],
    since: date,
    as_of_date: date,
    *,
    source_id: str,
    source_dimension: str | None,
    source_strength: SourceStrength,
) -> list[SourceDocument]:
    payload = web_search_client.generate_json_with_web_search(
        _pe_deal_web_search_prompt(pe_firm, target_profile, same_terms, adjacent_terms, since, as_of_date),
        _PE_DEAL_WEB_SEARCH_SCHEMA_NAME,
        _PE_DEAL_WEB_SEARCH_JSON_SCHEMA,
        system_prompt=_pe_deal_web_search_system_prompt(),
        max_results=retrieval_config.web_search_max_results,
        max_total_results=retrieval_config.web_search_max_total_results,
        search_engine=retrieval_config.web_search_engine,
        search_context_size=retrieval_config.web_search_context_size,
        source_business_type=_PE_DEAL_WEB_SEARCH_BUSINESS_TYPE,
    )
    output = _PEDealWebSearchOutput.model_validate(_normalize_web_search_payload(payload))
    documents: list[SourceDocument] = []
    for deal in output.deals:
        if not deal.has_relevant_deal or deal.sector_relevance == "unrelated":
            continue
        if not deal.source_url or not deal.acquired_company:
            continue
        document = SourceDocument(
            source_id=source_id,
            source_dimension=source_dimension,
            source_type=SourceType.transaction_signal,
            source_strength=source_strength,
            url=deal.source_url,
            raw_text=deal.evidence_summary or f"{pe_firm} had a deal involving {deal.acquired_company}.",
            metadata={
                "discovered_from": "llm_web_search",
                "source_path": _WEB_SEARCH_SOURCE_PATH,
                "llm_schema_name": _PE_DEAL_WEB_SEARCH_SCHEMA_NAME,
                "acquiringCompanyName": pe_firm,
                "acquiredCompanyName": deal.acquired_company,
                "transactionDate": deal.acquisition_date,
                "transactionType": deal.deal_type,
                "sector_relevance": deal.sector_relevance,
                "source_title": deal.source_title,
                "sic": normalize_sic(target_profile.sic),
                "lookback_start": since.isoformat(),
                "lookback_end": as_of_date.isoformat(),
            },
        )
        documents.append(document)
    return documents


def _normalize_web_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept common LLM field variants so useful sourced deals are not dropped by naming drift."""

    normalized = dict(payload)
    if "deals" not in normalized:
        for key in ("acquisitions", "transactions", "results", "records", "deal_activity"):
            value = normalized.get(key)
            if isinstance(value, list):
                normalized["deals"] = value
                break
        deal = normalized.get("deal")
        if "deals" not in normalized and isinstance(deal, dict):
            normalized["deals"] = [deal]
    deals = normalized.get("deals")
    if isinstance(deals, list):
        normalized["deals"] = [_normalize_web_search_deal(deal) for deal in deals if isinstance(deal, dict)]
    return normalized


def _normalize_web_search_deal(deal: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(deal)
    if not first_text(normalized, "source_url", "url", "link", "source_link"):
        source_url, source_title = _first_source_reference(normalized)
        if source_url:
            normalized["source_url"] = source_url
        if source_title and not first_text(normalized, "source_title", "title", "headline"):
            normalized["source_title"] = source_title
    if "sector_relevance" not in normalized and "relevance" not in normalized:
        normalized["sector_relevance"] = "same"
    else:
        raw_relevance = first_text(normalized, "sector_relevance", "relevance")
        normalized["sector_relevance"] = _normalize_sector_relevance(raw_relevance) or "same"
    return normalized


def _normalize_sector_relevance(value: str | None) -> str | None:
    """Collapse explanatory LLM relevance labels into the retriever's canonical buckets."""

    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if re.search(r"\bunrelated\b|\bnot\s+related\b|\bno\s+relevance\b", normalized):
        return "unrelated"
    if re.search(r"\badjacent\b|\brelated\b|\bneighboring\b", normalized):
        return "adjacent"
    if re.search(r"\bsame\b|\bsame-industry\b|\bsame\s+industry\b|\bdirect\b", normalized):
        return "same"
    return None


def _first_source_reference(value: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("source_links", "source_urls", "links", "urls"):
        items = value.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.strip():
                    return item.strip(), None
                if isinstance(item, dict):
                    url = first_text(item, "source_url", "url", "link", "href")
                    title = first_text(item, "source_title", "title", "name")
                    if url:
                        return url, title
    source = value.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip(), None
    if isinstance(source, dict):
        return first_text(source, "source_url", "url", "link", "href"), first_text(source, "source_title", "title", "name")
    return None, None


def _pe_deal_web_search_prompt(
    pe_firm: str,
    target_profile: TargetProfile,
    same_terms: list[str],
    adjacent_terms: list[str],
    since: date,
    as_of_date: date,
) -> str:
    sic = normalize_sic(target_profile.sic) or "unknown"
    same_text = ", ".join(same_terms[:10]) or "not available"
    adjacent_text = ", ".join(adjacent_terms[:10]) or "not available"
    return (
        "Use web search. "
        f"Find whether {pe_firm} has acquisition, buyout, add-on, or investment records in the past five years "
        f"for SIC={sic}, from {since.isoformat()} through {as_of_date.isoformat()}. "
        f"Industry context only: same-industry terms: {same_text}. "
        f"Adjacent-industry terms: {adjacent_text}. "
        "Use only the SIC and industry terms as search guidance; do not use a specific seller company name. "
        "The source page does not need to literally mention the SIC code. "
        "Return deals where the source page supports the PE firm, acquired company, approximate acquisition date, "
        "source URL, and relevance to the same or adjacent industry. Prefer official PE announcements, portfolio pages, "
        "company press releases, and reputable news. Do not include broad fund news without a named acquired company."
    )


def _pe_deal_web_search_system_prompt() -> str:
    return (
        f"Return only valid JSON for schema {_PE_DEAL_WEB_SEARCH_SCHEMA_NAME}. "
        "The JSON object must contain pe_firm, sic, and deals. Each deal must include has_relevant_deal, "
        "evidence_summary, acquisition_date, acquired_company, source_url, source_title, sector_relevance, and deal_type. "
        "Use null for unknown optional values and an empty deals array when no relevant sourced deal is found."
    )


def _industry_query_terms(target_profile: TargetProfile, max_queries: int, taxonomy_path: Any) -> tuple[list[str], list[str], list[str]]:
    taxonomy_terms = _sic_taxonomy_terms(taxonomy_path, normalize_sic(target_profile.sic))
    same_terms = unique_terms([*taxonomy_terms, *target_profile.products, *target_profile.keywords])
    adjacent_terms = unique_terms([*target_profile.adjacent_categories, *target_profile.customer_segments, *target_profile.channels])
    query_terms = unique_terms([*same_terms, *adjacent_terms])
    if not query_terms:
        query_terms = [target_profile.name]
    return query_terms[:max_queries], same_terms, adjacent_terms


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


def _extract_raw_acquirer(document: SourceDocument) -> str | None:
    acquirer = first_text(
        document.metadata,
        "acquiringCompanyName",
        "acquirerName",
        "buyer",
        "buyerName",
        "acquiring_company",
        "companyName",
    )
    if acquirer:
        return acquirer
    buyer, _target = _parse_buyer_and_target(_document_text(document))
    return buyer


def _extract_acquired_target(document: SourceDocument) -> str | None:
    target = first_text(
        document.metadata,
        "acquiredCompanyName",
        "targetCompanyName",
        "target",
        "sellerName",
        "acquired_company",
    )
    if target:
        return target
    _buyer, parsed_target = _parse_buyer_and_target(_document_text(document))
    return parsed_target


def _document_date(document: SourceDocument) -> str | None:
    for key in ("transactionDate", "date", "filingDate", "acceptedDate", "published_at", "publishedAt"):
        value = document.metadata.get(key)
        parsed = parse_date(value) or _parse_approximate_date(value)
        if parsed:
            return parsed.isoformat()
    return None


def _parse_approximate_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    year_match = re.fullmatch(r"(?P<year>20\d{2}|19\d{2})", text)
    if year_match:
        return date(int(year_match.group("year")), 1, 1)
    year_month_match = re.fullmatch(r"(?P<year>20\d{2}|19\d{2})[-/](?P<month>\d{1,2})", text)
    if year_month_match:
        month = max(1, min(12, int(year_month_match.group("month"))))
        return date(int(year_month_match.group("year")), month, 1)
    quarter_match = re.search(r"\bQ(?P<quarter>[1-4])\s+(?P<year>20\d{2}|19\d{2})\b", text, flags=re.IGNORECASE)
    if quarter_match:
        return date(int(quarter_match.group("year")), (int(quarter_match.group("quarter")) - 1) * 3 + 1, 1)
    month_match = re.search(
        r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
        r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<year>20\d{2}|19\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_match:
        return date(int(month_match.group("year")), _MONTHS[month_match.group("month")[:3].casefold()], 1)
    return None


def _deal_type(document: SourceDocument) -> str:
    structured = first_text(document.metadata, "transactionType", "dealType", "type")
    if structured:
        return structured.strip()[:40]
    lowered = _document_text(document).casefold()
    if "recapitalization" in lowered or "recap" in lowered:
        return "recapitalization"
    if "buyout" in lowered:
        return "buyout"
    if "add-on" in lowered or "addon" in lowered:
        return "add-on"
    if "investment" in lowered or "invests" in lowered:
        return "investment"
    return "acquisition"


def _sector_match(text: str, same_terms: list[str], adjacent_terms: list[str], target_profile: TargetProfile) -> str:
    lowered = text.casefold()
    if any(_term_in_text(term, lowered) for term in same_terms):
        return "same"
    if any(_term_in_text(term, lowered) for term in adjacent_terms):
        return "adjacent"
    # When no richer profile terms exist, the target name is a weak but traceable last-resort industry anchor.
    if not same_terms and not adjacent_terms and _term_in_text(target_profile.name, lowered):
        return "same"
    return "unrelated"


def _matched_seed(document: SourceDocument, matcher: PESeedMatcher) -> bool:
    raw_acquirer = _extract_raw_acquirer(document)
    return bool(raw_acquirer and (matcher.match(_clean_acquirer_name(raw_acquirer)) or matcher.match(raw_acquirer)))


def _clean_acquirer_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" .,:;-'\"")
    # FMP/news prose can wrap sponsor names in fund-management phrases; remove the wrapper before seed matching.
    text = re.sub(
        r"^(?:affiliates?|funds?|investment funds?|vehicles?)\s+(?:managed|advised|controlled)\s+by\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^funds?\s+affiliated\s+with\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" .,:;-'\"")


def _document_has_transaction_language(document: SourceDocument, transaction_terms: list[str]) -> bool:
    metadata = document.metadata
    if first_text(metadata, "transactionId", "transactionDate", "acquiredCompanyName", "targetCompanyName"):
        return True
    lowered = _document_text(document).casefold()
    return any(_term_in_text(term, lowered) for term in transaction_terms)


def _document_text(document: SourceDocument) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    if document.raw_text:
        compact = re.sub(r"\s+", " ", document.raw_text).strip()
        if compact:
            parts.append(compact)
            seen.add(compact.casefold())
    for key in (
        "acquiringCompanyName",
        "acquiredCompanyName",
        "targetCompanyName",
        "transactionType",
        "dealType",
        "title",
        "description",
    ):
        value = document.metadata.get(key)
        if isinstance(value, str):
            compact = re.sub(r"\s+", " ", value).strip()
            if compact and compact.casefold() not in seen:
                parts.append(compact)
                seen.add(compact.casefold())
    return " ".join(parts)


def _parse_buyer_and_target(text: str) -> tuple[str | None, str | None]:
    for pattern in _DEAL_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        buyer = _clean_entity(match.group("buyer"))
        target = _clean_entity(match.group("target")) if "target" in match.groupdict() else None
        if buyer:
            return buyer, target
    return None, None


def _clean_entity(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip(" .,:;-'\"")
    for separator in (", a ", ", an ", " to ", " for ", " from ", " after ", " as ", " in ", " on ", " with ", " | ", " - "):
        index = text.casefold().find(separator)
        if index > 0:
            text = text[:index].strip(" .,:;-'\"")
    return text or None


def _annotate_query_documents(documents: list[SourceDocument], query: str, source_path: str) -> list[SourceDocument]:
    annotated: list[SourceDocument] = []
    for document in documents:
        metadata = {**document.metadata, "fmp_query": query, "source_path": source_path}
        annotated.append(document.model_copy(update={"metadata": metadata}))
    return annotated


def _dedupe_documents(documents: list[SourceDocument]) -> list[SourceDocument]:
    seen: set[str] = set()
    deduped: list[SourceDocument] = []
    for document in documents:
        key = _document_key(document)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(document)
    return deduped


def _is_llm_web_search_document(document: SourceDocument) -> bool:
    """Identify LLM web-search evidence independently from the concrete LLM provider."""

    return document.source_id == _LLM_WEB_SEARCH_SOURCE_ID or document.metadata.get("discovered_from") == "llm_web_search"


def _cap_documents_preserving_sources(documents: list[SourceDocument], max_documents: int) -> list[SourceDocument]:
    """Apply the retriever-level document cap while keeping LLM web-search evidence from being starved by FMP volume."""

    if len(documents) <= max_documents:
        return documents

    web_documents = [document for document in documents if _is_llm_web_search_document(document)]
    other_documents = [document for document in documents if not _is_llm_web_search_document(document)]
    if not web_documents or not other_documents:
        return documents[:max_documents]

    web_budget = min(len(web_documents), max(1, max_documents // 4))
    other_budget = max_documents - web_budget
    selected = [*other_documents[:other_budget], *web_documents[:web_budget]]
    selected_keys = {_document_key(document) for document in selected}
    if len(selected) < max_documents:
        for document in documents:
            key = _document_key(document)
            if key in selected_keys:
                continue
            selected.append(document)
            selected_keys.add(key)
            if len(selected) >= max_documents:
                break
    return selected


def _document_key(document: SourceDocument) -> str:
    metadata = document.metadata
    return (
        first_text(metadata, "transactionId", "dealId", "id")
        or document.url
        or "|".join(
            [
                first_text(metadata, "acquiringCompanyName", "acquirerName", "buyer") or "",
                first_text(metadata, "acquiredCompanyName", "targetCompanyName", "target") or "",
                first_text(metadata, "transactionDate", "date", "filingDate") or "",
            ]
        )
    )


def _term_in_text(term: str, lowered_text: str) -> bool:
    normalized = term.strip().casefold()
    return bool(normalized) and normalized in lowered_text


def _snippet(text: str, max_chars: int = 280) -> str:
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _safe_error_message(error: Exception) -> str:
    return re.sub(r"apikey=[^&\s']+", "apikey=<redacted>", str(error))


_BUYER_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,120}?"
_TARGET_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,120}"
_DEAL_PATTERNS = [
    re.compile(
        rf"(?P<buyer>{_BUYER_ENTITY})\s+(?:has\s+|had\s+|will\s+|to\s+)?"
        rf"(?:acquired|acquires|acquire|buys|bought|invested\s+in)\s+(?P<target>{_TARGET_ENTITY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<buyer>{_BUYER_ENTITY})\s+(?:announced|announces|completed|completes)\s+"
        rf"(?:the\s+)?(?:acquisition|investment)\s+(?:of|in)\s+(?P<target>{_TARGET_ENTITY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<target>{_TARGET_ENTITY})\s+(?:was\s+|is\s+)?(?:acquired|bought)\s+by\s+(?P<buyer>{_BUYER_ENTITY})",
        re.IGNORECASE,
    ),
]
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
