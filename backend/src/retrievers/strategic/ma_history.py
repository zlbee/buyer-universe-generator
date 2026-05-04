"""M&A-history strategic buyer retriever."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.domain import BuyerType, CandidateHit, DealEvent, Evidence, ResolvedTarget, SourceDocument, SourceType, StrategicRetrievalResult, TargetProfile
from src.retrievers.strategic.common import (
    calendar_years_before,
    first_text,
    normalize_cik,
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
        max_companies = required_positive_int(config, "max_companies", self.name, warnings)
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
        if config.require_edgar_primary_item and not config.edgar_primary_items:
            warnings.append(f"{self.name} skipped: retriever policy requires EDGAR primary items but none are configured")
        if not config.transaction_terms:
            warnings.append(f"{self.name} skipped: retriever policy has no transaction_terms")
        if (
            lookback_years is None
            or max_companies is None
            or max_documents is None
            or max_queries is None
            or page_size is None
            or not primary_source_id
            or not eligible_sector_matches
            or not config.edgar_form_type
            or (config.require_edgar_primary_item and not config.edgar_primary_items)
            or not config.transaction_terms
        ):
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        since = calendar_years_before(self.as_of_date, lookback_years)
        documents: list[SourceDocument] = []
        queries: list[str] = []
        news_from_date: date | None = None
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
                            company_limit=max_companies,
                            source_strength=edgar_source.source_strength,
                            source_dimension=edgar_source.dimension_id,
                            primary_items=config.edgar_primary_items,
                            supporting_items=config.edgar_supporting_items,
                            require_primary_item=config.require_edgar_primary_item,
                            fetch_filing_text=config.fetch_edgar_filing_text,
                            text_scope=config.edgar_text_scope,
                            as_of_date=self.as_of_date,
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
                    news_from_date = _provider_from_date(since, self.as_of_date, news_source.config.retrieval.max_lookback_days)
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
                                from_date=news_from_date.isoformat(),
                            )
                            news_document_count += len(news_documents)
                            documents.extend(news_documents)
                        except Exception as error:
                            warnings.append(f"{self.name} NewsAPI query failed: {type(error).__name__}: {_safe_error_message(error)}")
                elif news_source and not self.newsapi_client:
                    warnings.append(f"{self.name} {source_id} unavailable: adapter unavailable")

        unique_documents = _dedupe_documents(documents)
        grouped_events: dict[str, list[tuple[DealEvent, Evidence, SourceDocument]]] = defaultdict(list)
        skipped_unrelated = 0
        skipped_old = 0
        skipped_without_buyer = 0
        skipped_without_transaction_language = 0
        for document in unique_documents:
            event = _deal_event_from_document(document, target_profile)
            if not event:
                if _document_has_transaction_language(document):
                    skipped_without_buyer += 1
                else:
                    skipped_without_transaction_language += 1
                continue
            event_date = _date_from_event(event)
            if event_date and event_date < since:
                skipped_old += 1
                continue
            if event.sector_match not in eligible_sector_matches:
                skipped_unrelated += 1
                continue
            grouped_events[normalize_entity_key(event.buyer)].append((event, _evidence_from_deal_event(event, document), document))

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
                "edgar_form_type": config.edgar_form_type,
                "edgar_primary_items": config.edgar_primary_items,
                "edgar_supporting_items": config.edgar_supporting_items,
                "require_edgar_primary_item": config.require_edgar_primary_item,
                "fetch_edgar_filing_text": config.fetch_edgar_filing_text,
                "edgar_text_scope": config.edgar_text_scope,
                "eligible_sector_matches": sorted(eligible_sector_matches),
                "lookback_start": since.isoformat(),
                "lookback_end": self.as_of_date.isoformat(),
                "queries": queries,
                "edgar_documents_checked": edgar_document_count,
                "news_documents_checked": news_document_count,
                "news_lookback_start": news_from_date.isoformat() if news_from_date else None,
                "documents_checked": len(unique_documents),
                "skip_reasons": {
                    "no_transaction_language": skipped_without_transaction_language,
                    "unparsed_buyer": skipped_without_buyer,
                    "older_than_lookback": skipped_old,
                    "unrelated_sector": skipped_unrelated,
                },
                "hit_count": len(hits),
            },
        )


def _mna_hit_from_events(
    event_group: list[tuple[DealEvent, Evidence, SourceDocument]],
    retriever_name: str,
    source_path: str,
    since: date,
) -> CandidateHit:
    events = [event for event, _evidence, _document in event_group]
    evidence = [event_evidence for _event, event_evidence, _document in event_group]
    documents = [document for _event, _evidence, document in event_group]
    buyer = events[0].buyer
    candidate_ticker, candidate_cik, candidate_domain = _candidate_identity_from_documents(documents)
    same_count = sum(1 for event in events if event.sector_match == "same")
    adjacent_count = sum(1 for event in events if event.sector_match == "adjacent")
    fit_reason = _mna_fit_reason(same_count, adjacent_count, since)
    base_confidence = 0.78 if same_count else 0.68
    confidence = min(0.92, base_confidence + max(0, len(events) - 1) * 0.04)
    return CandidateHit(
        candidate_name=buyer,
        candidate_ticker=candidate_ticker,
        candidate_cik=candidate_cik,
        candidate_domain=candidate_domain,
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
            "candidate_ticker": candidate_ticker,
            "candidate_cik": candidate_cik,
            "candidate_domain": candidate_domain,
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
    is_sec_item_signal = _is_sec_item_acquisition_signal(document)
    if not _looks_like_transaction(text) and not is_sec_item_signal:
        return None

    if document.source_type == SourceType.sec_filing and is_sec_item_signal:
        # In 8-K Item 2.01 filings the SEC filer is the acquirer; prose often starts with generic fragments
        # such as "the Company completed..." that should never become buyer names.
        buyer = _filing_buyer_from_document(document)
        acquired_target = _parse_acquired_target(text)
    else:
        buyer, acquired_target = _parse_buyer_and_target(text)
    if not buyer:
        return None
    if _is_bad_buyer_name(buyer):
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
        quote_or_snippet=_snippet(text or _sec_item_signal_summary(document)),
    )


def _evidence_from_deal_event(event: DealEvent, document: SourceDocument) -> Evidence:
    target_text = f" involving {event.target_acquired}" if event.target_acquired else ""
    item_text = _sec_item_claim_fragment(document)
    if item_text:
        claim = f"{event.buyer} filed an {item_text} signal{target_text} with {event.sector_match} sector relevance."
    else:
        claim = (
            f"{event.buyer} had a {event.deal_type or 'transaction'} signal"
            f"{target_text} with {event.sector_match} sector relevance."
        )
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


def _provider_from_date(retriever_since: date, as_of_date: date, max_lookback_days: int | None) -> date:
    if not max_lookback_days:
        return retriever_since
    provider_since = as_of_date - timedelta(days=max_lookback_days)
    return max(retriever_since, provider_since)


def _safe_error_message(error: Exception) -> str:
    return re.sub(r"apiKey=[^&\s']+", "apiKey=<redacted>", str(error))


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


def _candidate_identity_from_documents(documents: list[SourceDocument]) -> tuple[str | None, str | None, str | None]:
    ticker: str | None = None
    cik: str | None = None
    domain: str | None = None
    for document in documents:
        metadata = document.metadata
        if not ticker:
            ticker = first_text(metadata, "ticker", "candidate_ticker", "target_ticker") or document.target_ticker
        if not cik:
            cik = normalize_cik(metadata.get("cik") or metadata.get("candidate_cik") or document.target_cik)
        if not domain:
            domain = first_text(metadata, "domain", "candidate_domain", "homepage_url", "primary_domain")

    # Some SEC fallback rows use the CIK as a ticker placeholder; exposing that twice is noisy in the UI.
    if ticker and cik and ticker.strip().lstrip("0") == cik.strip().lstrip("0"):
        ticker = None
    return ticker, cik, domain


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
    item_summary = _sec_item_signal_summary(document)
    if item_summary and item_summary.casefold() not in seen_parts:
        parts.append(item_summary)
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


def _parse_acquired_target(text: str) -> str | None:
    for pattern in _TARGET_ONLY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        target = _clean_entity(match.group("target"))
        if target and not _is_bad_buyer_name(target):
            return target
    return None


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
        item_fragment = _sec_item_claim_fragment(document)
        if item_fragment:
            return item_fragment
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


def _is_sec_item_acquisition_signal(document: SourceDocument) -> bool:
    if document.source_type != SourceType.sec_filing:
        return False
    item_match = document.metadata.get("edgar_item_match")
    if isinstance(item_match, dict) and item_match.get("primary"):
        return True
    return "2.01" in _metadata_item_codes(document)


def _metadata_item_codes(document: SourceDocument) -> set[str]:
    item_values = document.metadata.get("filing_items") or document.metadata.get("items")
    if not item_values:
        return set()
    if isinstance(item_values, str):
        candidates = re.split(r"[,;|]+", item_values)
    elif isinstance(item_values, list):
        candidates = [str(item) for item in item_values]
    else:
        candidates = [str(item_values)]
    return {_normalize_item_code(candidate) for candidate in candidates if _normalize_item_code(candidate)}


def _normalize_item_code(value: str) -> str | None:
    match = re.search(r"\b(?P<major>\d{1,2})\s*\.\s*(?P<minor>\d{2})\b", value)
    if not match:
        return None
    return f"{int(match.group('major'))}.{match.group('minor')}"


def _filing_buyer_from_document(document: SourceDocument) -> str | None:
    for key in ("canonical_name", "company_name", "filer_name"):
        value = document.metadata.get(key)
        if isinstance(value, str):
            cleaned = _clean_entity(value)
            if cleaned:
                return cleaned
    title = document.metadata.get("title")
    if isinstance(title, str):
        cleaned = _clean_entity(re.sub(r"^\s*8-K(?:/A)?\s*[-:]\s*", "", title, flags=re.IGNORECASE))
        if cleaned:
            return cleaned
    if document.target_cik:
        return f"CIK {document.target_cik}"
    return None


def _is_generic_buyer_reference(value: str | None) -> bool:
    if not value:
        return False
    normalized = re.sub(r"[^a-z]+", " ", value.casefold()).strip()
    return normalized in {"the company", "company", "registrant", "the registrant", "issuer", "we", "us"}


def _is_bad_buyer_name(value: str | None) -> bool:
    if not value:
        return True
    text = re.sub(r"\s+", " ", value).strip()
    lowered = text.casefold()
    if not text or text[0].islower():
        return True
    if _normalize_item_code(text) or re.search(r"\bitem\s+\d{1,2}\.\d{2}\b", lowered):
        return True
    bad_fragments = (
        "completion of acquisition",
        "disposition of assets",
        "completed the",
        "previously announced",
        "which were",
        "were settled",
        "immediately prior",
        "financial statements",
        "exhibits",
    )
    return any(fragment in lowered for fragment in bad_fragments)


def _sec_item_signal_summary(document: SourceDocument) -> str | None:
    if document.source_type != SourceType.sec_filing:
        return None
    item_match = document.metadata.get("edgar_item_match")
    primary_items: list[str] = []
    supporting_items: list[str] = []
    if isinstance(item_match, dict):
        primary_items = [str(item) for item in item_match.get("primary", [])]
        supporting_items = [str(item) for item in item_match.get("supporting", [])]
    elif "2.01" in _metadata_item_codes(document):
        primary_items = ["2.01"]
    if not primary_items and not supporting_items:
        return None
    primary_text = ", ".join(primary_items)
    supporting_text = f"; supporting Item(s) {', '.join(supporting_items)}" if supporting_items else ""
    return f"SEC 8-K Item(s) {primary_text}{supporting_text}: Completion of Acquisition or Disposition of Assets."


def _sec_item_claim_fragment(document: SourceDocument) -> str | None:
    if not _is_sec_item_acquisition_signal(document):
        return None
    item_match = document.metadata.get("edgar_item_match")
    primary_items = item_match.get("primary", []) if isinstance(item_match, dict) else ["2.01"]
    if primary_items:
        return f"8-K Item {', '.join(str(item) for item in primary_items)} acquisition completion"
    return "8-K acquisition completion"


def _snippet(text: str, max_chars: int = 280) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:max_chars]


_TRANSACTION_RE = re.compile(
    r"\b(acquir(?:e|es|ed|ing)|acquisition|buys|bought|merger|add-on|addon|investment)\b",
    re.IGNORECASE,
)

_BUYER_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,100}?"
_TARGET_ENTITY = r"[A-Z][A-Za-z0-9&.,'() -]{1,100}"
_TARGET_ENTITY_STOPPED = r"[A-Z][A-Za-z0-9&,'() -]{1,100}?"
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
_TARGET_ONLY_PATTERNS = [
    re.compile(
        rf"\b(?:completed\s+(?:the\s+)?(?:previously\s+announced\s+)?|announced\s+(?:the\s+)?)?"
        rf"acquisition\s+of\s+(?P<target>{_TARGET_ENTITY_STOPPED})(?=[.,;]|\s+which\b|\s+that\b|$)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:acquired|acquires|acquire|bought|buys)\s+(?P<target>{_TARGET_ENTITY_STOPPED})(?=[.,;]|\s+which\b|\s+that\b|$)",
        re.IGNORECASE,
    ),
]
