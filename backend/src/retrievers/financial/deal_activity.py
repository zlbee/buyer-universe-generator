"""FMP-backed PE deal activity retriever."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.domain import BuyerType, CandidateHit, DealEvent, Evidence, SourceDocument, StrategicRetrievalResult, TargetProfile
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


class PEDealActivityRetriever:
    """Recalls financial buyers with recent same/adjacent-industry FMP deal signals."""

    name = "PEDealActivityRetriever"
    source_path = "pe_deal_activity"

    def __init__(
        self,
        strategy: DataSourceStrategy,
        *,
        fmp_client: PEDealActivitySource | None = None,
        seed_universe: PESeedUniverse | None = None,
        as_of_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.fmp_client = fmp_client
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

        matcher = PESeedMatcher(seed_universe)
        since = calendar_years_before(self.as_of_date, lookback_years)
        query_terms, same_terms, adjacent_terms = _industry_query_terms(
            target_profile,
            max_queries,
            self.strategy.settings.keyword_taxonomy_path,
        )
        documents: list[SourceDocument] = []
        fmp_document_count = 0

        for source_id in source_execution_order(config):
            if source_id != deal_source_id:
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
                    documents.extend(_annotate_query_documents(page_documents, query))
                    if len(page_documents) < page_size:
                        break
                    page += 1

        unique_documents = _dedupe_documents(documents[:max_documents])
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

        hits = [_hit_from_signals(signal_group, self.name, self.source_path, since) for signal_group in grouped_signals.values()]
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
                "fmp_documents_checked": fmp_document_count,
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


def _hit_from_signals(signals: list[_PEDealSignal], retriever_name: str, source_path: str, since: date) -> CandidateHit:
    firm = signals[0].seed_match.firm
    events = [signal.event for signal in signals]
    evidence = [signal.evidence for signal in signals]
    same_count = sum(1 for event in events if event.sector_match == "same")
    adjacent_count = sum(1 for event in events if event.sector_match == "adjacent")
    base_confidence = 0.76 if same_count else 0.66
    confidence = min(0.9, base_confidence + max(0, len(events) - 1) * 0.04)

    return CandidateHit(
        candidate_name=firm.canonical_name,
        candidate_domain=firm.domain,
        buyer_type=BuyerType.financial,
        retriever_name=retriever_name,
        source_path=[source_path],
        fit_reason=_financial_fit_reason(same_count, adjacent_count, since),
        evidence=evidence,
        confidence=confidence,
        retrieval_metadata={
            "lookback_start": since.isoformat(),
            "matched_seed_firm": firm.canonical_name,
            "matched_aliases": unique_terms([signal.seed_match.matched_name for signal in signals]),
            "raw_acquirers": unique_terms([signal.raw_acquirer for signal in signals]),
            "query_terms": unique_terms([signal.query_term or "" for signal in signals]),
            "deal_events": [event.model_dump(mode="json") for event in events],
            "same_sector_event_count": same_count,
            "adjacent_sector_event_count": adjacent_count,
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
    sector_match = _sector_match(text, same_terms, adjacent_terms, target_profile)
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
        quote_or_snippet=_snippet(text),
    )
    return _PEDealSignal(
        event=event,
        evidence=_evidence_from_event(event, document),
        document=document,
        seed_match=seed_match,
        raw_acquirer=raw_acquirer,
        query_term=first_text(document.metadata, "fmp_query"),
    )


def _evidence_from_event(event: DealEvent, document: SourceDocument) -> Evidence:
    target_text = f" involving {event.target_acquired}" if event.target_acquired else ""
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
        verified_fact=True,
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
        parsed = parse_date(document.metadata.get(key))
        if parsed:
            return parsed.isoformat()
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


def _annotate_query_documents(documents: list[SourceDocument], query: str) -> list[SourceDocument]:
    annotated: list[SourceDocument] = []
    for document in documents:
        metadata = {**document.metadata, "fmp_query": query}
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
