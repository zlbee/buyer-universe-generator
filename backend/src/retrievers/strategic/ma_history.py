"""M&A-history strategic buyer retriever."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from src.domain import BuyerType, CandidateHit, DealEvent, Evidence, ResolvedTarget, SourceDocument, SourceType, StrategicRetrievalResult, TargetProfile
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
    source_label,
    source_type_value,
    unique_terms,
)
from src.retrievers.strategic.protocols import TransactionFilingSource, TransactionNewsSource
from src.sources.strategy import DataSourceStrategy


class MAHistoryRetriever:
    """Recalls strategic buyers with recent same/adjacent-sector acquisition history."""

    name = "MAHistoryRetriever"
    source_path = "mna_history"

    def __init__(
        self,
        strategy: DataSourceStrategy,
        *,
        newsapi_client: TransactionNewsSource | None = None,
        edgar_client: TransactionFilingSource | None = None,
        as_of_date: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.newsapi_client = newsapi_client
        self.edgar_client = edgar_client
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
        primary_source_id = required_source_role(config, "primary_filing_source", self.name, warnings)
        supplemental_news_source_id = config.source_roles.get("supplemental_news_source")
        eligible_sector_matches = set(config.eligible_sector_matches)
        if not eligible_sector_matches:
            warnings.append(f"{self.name} skipped: retriever policy has no eligible_sector_matches")
        if not config.edgar_form_type:
            warnings.append(f"{self.name} skipped: retriever policy has no edgar_form_type")
        if not config.transaction_terms:
            warnings.append(f"{self.name} skipped: retriever policy has no transaction_terms")
        if (
            lookback_years is None
            or max_documents is None
            or max_queries is None
            or page_size is None
            or not primary_source_id
            or not eligible_sector_matches
            or not config.edgar_form_type
            or not config.transaction_terms
        ):
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        since = calendar_years_before(self.as_of_date, lookback_years)
        documents: list[SourceDocument] = []
        queries: list[str] = []
        edgar_document_count = 0
        news_document_count = 0

        for source_id in source_execution_order(config):
            if source_id == primary_source_id:
                edgar_source = selected_source(self.strategy, config.use_case, source_id, self.name, warnings, required=False)
                if edgar_source and self.edgar_client:
                    try:
                        edgar_documents = self.edgar_client.fetch_transaction_signal_documents(
                            target_profile,
                            since,
                            self.strategy.cache_ttl_hours(source_id),
                            form_type=config.edgar_form_type,
                            limit=max_documents,
                            source_strength=edgar_source.source_strength,
                            source_dimension=edgar_source.dimension_id,
                        )
                        edgar_document_count = len(edgar_documents)
                        documents.extend(edgar_documents)
                    except Exception as error:
                        warnings.append(f"{self.name} EDGAR transaction retrieval failed: {type(error).__name__}: {error}")
                elif edgar_source and not self.edgar_client:
                    warnings.append(f"{self.name} {source_id} unavailable: adapter unavailable")
            elif source_id == supplemental_news_source_id:
                news_source = selected_source(self.strategy, config.use_case, source_id, self.name, warnings, required=False)
                if news_source and self.newsapi_client:
                    target = _target_profile_as_resolved(target_profile)
                    queries = _transaction_queries(target_profile, max_queries, config.transaction_terms)
                    for query in queries:
                        try:
                            news_documents = self.newsapi_client.fetch_articles_for_query(
                                query,
                                target,
                                self.strategy.cache_ttl_hours(source_id),
                                page_size=page_size,
                                source_strength=news_source.source_strength,
                                source_dimension=news_source.dimension_id,
                                domains=news_source.config.retrieval.domains,
                                from_date=since.isoformat(),
                            )
                            news_document_count += len(news_documents)
                            documents.extend(news_documents)
                        except Exception as error:
                            warnings.append(f"{self.name} NewsAPI query failed: {type(error).__name__}: {error}")
                elif news_source and not self.newsapi_client:
                    warnings.append(f"{self.name} {source_id} unavailable: adapter unavailable")

        unique_documents = _dedupe_documents(documents)
        grouped_events: dict[str, list[tuple[DealEvent, Evidence]]] = defaultdict(list)
        skipped_unrelated = 0
        skipped_old = 0
        skipped_without_buyer = 0
        for document in unique_documents:
            event = _deal_event_from_document(document, target_profile)
            if not event:
                if _document_has_transaction_language(document):
                    skipped_without_buyer += 1
                continue
            event_date = _date_from_event(event)
            if event_date and event_date < since:
                skipped_old += 1
                continue
            if event.sector_match not in eligible_sector_matches:
                skipped_unrelated += 1
                continue
            grouped_events[normalize_entity_key(event.buyer)].append((event, _evidence_from_deal_event(event, document)))

        hits = [
            _mna_hit_from_events(event_group, self.name, self.source_path, since)
            for event_group in grouped_events.values()
            if event_group
        ]
        hits.sort(key=lambda hit: (-hit.confidence, hit.candidate_name.casefold()))

        if skipped_without_buyer:
            warnings.append(f"{self.name} skipped {skipped_without_buyer} transaction documents without a parsed buyer")
        if skipped_old:
            warnings.append(f"{self.name} skipped {skipped_old} transaction events older than {since.isoformat()}")
        if skipped_unrelated:
            warnings.append(f"{self.name} skipped {skipped_unrelated} unrelated transaction events")

        return StrategicRetrievalResult(
            hits=hits,
            warnings=warnings,
            metadata={
                "retriever": self.name,
                "source_use_case": config.use_case,
                "source_roles": config.source_roles,
                "source_priority": config.source_priority,
                "primary_source": source_label(primary_source_id, config.edgar_form_type),
                "secondary_source": supplemental_news_source_id,
                "eligible_sector_matches": sorted(eligible_sector_matches),
                "lookback_start": since.isoformat(),
                "lookback_end": self.as_of_date.isoformat(),
                "queries": queries,
                "edgar_documents_checked": edgar_document_count,
                "news_documents_checked": news_document_count,
                "documents_checked": len(unique_documents),
                "hit_count": len(hits),
            },
        )


def _mna_hit_from_events(
    event_group: list[tuple[DealEvent, Evidence]],
    retriever_name: str,
    source_path: str,
    since: date,
) -> CandidateHit:
    events = [event for event, _evidence in event_group]
    evidence = [event_evidence for _event, event_evidence in event_group]
    buyer = events[0].buyer
    same_count = sum(1 for event in events if event.sector_match == "same")
    adjacent_count = sum(1 for event in events if event.sector_match == "adjacent")
    fit_reason = _mna_fit_reason(same_count, adjacent_count, since)
    base_confidence = 0.78 if same_count else 0.68
    confidence = min(0.92, base_confidence + max(0, len(events) - 1) * 0.04)
    return CandidateHit(
        candidate_name=buyer,
        buyer_type=BuyerType.strategic,
        retriever_name=retriever_name,
        source_path=[source_path],
        fit_reason=fit_reason,
        evidence=evidence,
        confidence=confidence,
        retrieval_metadata={
            "lookback_start": since.isoformat(),
            "deal_events": [event.model_dump(mode="json") for event in events],
            "same_sector_event_count": same_count,
            "adjacent_sector_event_count": adjacent_count,
        },
    )


def _mna_fit_reason(same_count: int, adjacent_count: int, since: date) -> str:
    total = same_count + adjacent_count
    if same_count and adjacent_count:
        return f"Completed {total} same or adjacent-sector transaction signals since {since.isoformat()}."
    if same_count:
        return f"Completed {same_count} same-sector transaction signal(s) since {since.isoformat()}."
    return f"Completed {adjacent_count} adjacent-sector transaction signal(s) since {since.isoformat()}."


def _deal_event_from_document(document: SourceDocument, target_profile: TargetProfile) -> DealEvent | None:
    text = _document_text(document)
    if not _looks_like_transaction(text):
        return None

    buyer, acquired_target = _parse_buyer_and_target(text)
    if not buyer:
        return None

    sector_match = _sector_match(document, text, target_profile)
    source_type = _deal_source_type(document)
    return DealEvent(
        buyer=buyer,
        target_acquired=acquired_target,
        deal_date=_document_date(document),
        deal_type=_deal_type(text),
        sector_match=sector_match,
        source_type=source_type,
        url=document.url,
        filing_accession=document.filing_accession,
        quote_or_snippet=_snippet(text),
    )


def _evidence_from_deal_event(event: DealEvent, document: SourceDocument) -> Evidence:
    target_text = f" involving {event.target_acquired}" if event.target_acquired else ""
    claim = f"{event.buyer} had a {event.deal_type or 'transaction'} signal{target_text} with {event.sector_match} sector relevance."
    return Evidence(
        claim=claim,
        source_type=source_type_value(document.source_type),
        source_dimension=document.source_dimension,
        source_strength=document.source_strength,
        url=document.url,
        filing_accession=document.filing_accession,
        quote_or_snippet=event.quote_or_snippet,
        verified_fact=True,
    )


def _transaction_queries(target_profile: TargetProfile, max_queries: int, transaction_terms: list[str]) -> list[str]:
    seed_terms = unique_terms(
        [
            *target_profile.products,
            *target_profile.keywords,
            *target_profile.customer_segments,
            *target_profile.channels,
            *target_profile.adjacent_categories,
        ]
    )
    if not seed_terms and target_profile.sic:
        seed_terms = [f"SIC {target_profile.sic}"]
    if not seed_terms:
        seed_terms = [target_profile.name]

    transaction_clause = _boolean_or_clause(transaction_terms)
    return [f'"{term}" {transaction_clause}' for term in seed_terms[:max_queries]]


def _boolean_or_clause(terms: list[str]) -> str:
    quoted_terms = [f'"{term}"' if re.search(r"[^A-Za-z0-9]", term) else term for term in terms]
    return f"({' OR '.join(quoted_terms)})"


def _target_profile_as_resolved(target_profile: TargetProfile) -> ResolvedTarget:
    return ResolvedTarget(
        canonical_name=target_profile.name,
        ticker=target_profile.ticker,
        cik=target_profile.cik,
        exchange=target_profile.exchange,
        sic=target_profile.sic,
        resolution_confidence=1.0,
        matched_input=target_profile.ticker,
        source_provenance=["target_profile"],
    )


def _dedupe_documents(documents: list[SourceDocument]) -> list[SourceDocument]:
    seen: set[str] = set()
    deduped: list[SourceDocument] = []
    for document in documents:
        key = document.url or document.filing_accession or repr(sorted(document.metadata.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(document)
    return deduped


def _document_text(document: SourceDocument) -> str:
    parts: list[str] = []
    seen_parts: set[str] = set()
    if document.raw_text:
        compact_raw_text = re.sub(r"\s+", " ", document.raw_text).strip()
        if compact_raw_text:
            parts.append(compact_raw_text)
            seen_parts.add(compact_raw_text.casefold())
    for key in ("title", "description", "content", "summary", "headline"):
        value = document.metadata.get(key)
        if isinstance(value, str):
            compact_value = re.sub(r"\s+", " ", value).strip()
            if compact_value and compact_value.casefold() not in seen_parts:
                parts.append(compact_value)
                seen_parts.add(compact_value.casefold())
    source = document.metadata.get("source")
    if isinstance(source, dict) and isinstance(source.get("name"), str):
        compact_source_name = re.sub(r"\s+", " ", source["name"]).strip()
        if compact_source_name and compact_source_name.casefold() not in seen_parts:
            parts.append(compact_source_name)
    return " ".join(parts)


def _document_has_transaction_language(document: SourceDocument) -> bool:
    return _looks_like_transaction(_document_text(document))


def _looks_like_transaction(text: str) -> bool:
    return bool(text and _TRANSACTION_RE.search(text))


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


def _sector_match(document: SourceDocument, text: str, target_profile: TargetProfile) -> str:
    document_sic = normalize_sic(document.metadata.get("sic") or document.metadata.get("candidate_sic"))
    target_sic = normalize_sic(target_profile.sic)
    if document.source_type == SourceType.sec_filing and document_sic and document_sic == target_sic:
        # Same-SIC 8-K filings are selected from EDGAR before text parsing, so the filing metadata itself is
        # industry evidence even when the short SEC Atom summary does not repeat the target's product terms.
        return "same"

    lowered = text.casefold()
    same_terms = unique_terms([*target_profile.products, *target_profile.keywords])
    adjacent_terms = unique_terms([*target_profile.adjacent_categories, *target_profile.customer_segments, *target_profile.channels])
    if any(_term_in_text(term, lowered) for term in same_terms):
        return "same"
    if any(_term_in_text(term, lowered) for term in adjacent_terms):
        return "adjacent"
    return "unrelated"


def _term_in_text(term: str, lowered_text: str) -> bool:
    normalized = term.strip().casefold()
    return bool(normalized) and normalized in lowered_text


def _deal_type(text: str) -> str:
    lowered = text.casefold()
    if "merger" in lowered:
        return "merger"
    if "add-on" in lowered or "addon" in lowered:
        return "add-on"
    if "investment" in lowered or "invests" in lowered:
        return "investment"
    return "acquisition"


def _deal_source_type(document: SourceDocument) -> str:
    if document.source_type == SourceType.news_article:
        return "news"
    if document.source_type == SourceType.sec_filing:
        return str(document.metadata.get("form") or "sec_filing")
    return source_type_value(document.source_type)


def _document_date(document: SourceDocument) -> str | None:
    for key in ("publishedAt", "published_at", "filing_date", "deal_date"):
        value = document.metadata.get(key)
        parsed = parse_date(value)
        if parsed:
            return parsed.isoformat()
    if document.retrieved_at:
        return document.retrieved_at.date().isoformat()
    return None


def _date_from_event(event: DealEvent) -> date | None:
    return parse_date(event.deal_date)


def _clean_entity(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip(" .,:;-'\"")
    text = re.sub(r"^(?:Reuters|Bloomberg|CNBC|WSJ)\s*[-:]\s*", "", text, flags=re.IGNORECASE)
    for separator in (", a ", ", an ", " to ", " for ", " from ", " after ", " as ", " in ", " on ", " with ", " | ", " - "):
        index = text.casefold().find(separator)
        if index > 0:
            text = text[:index].strip(" .,:;-'\"")
    return text or None


def _snippet(text: str, max_chars: int = 280) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:max_chars]


_TRANSACTION_RE = re.compile(
    r"\b(acquir(?:e|es|ed|ing)|acquisition|buys|bought|merger|add-on|addon|investment)\b",
    re.IGNORECASE,
)

_BUYER_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,100}?"
_TARGET_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,100}"
_DEAL_PATTERNS = [
    re.compile(
        rf"(?P<buyer>{_BUYER_ENTITY})\s+(?:has\s+|had\s+|will\s+|to\s+)?"
        rf"(?:acquired|acquires|acquire|buys|bought)\s+(?P<target>{_TARGET_ENTITY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<buyer>{_BUYER_ENTITY})\s+(?:announced|announces|completed|completes)\s+"
        rf"(?:the\s+)?acquisition\s+of\s+(?P<target>{_TARGET_ENTITY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<target>{_TARGET_ENTITY})\s+(?:was\s+|is\s+)?(?:acquired|bought)\s+by\s+(?P<buyer>{_BUYER_ENTITY})",
        re.IGNORECASE,
    ),
]
