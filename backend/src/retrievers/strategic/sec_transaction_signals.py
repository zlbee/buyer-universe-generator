"""SEC-backed transaction-signal source for M&A-history retrieval."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.domain import FilingMetadata, ResolvedTarget, SourceDocument, SourceStrength, SourceType, TargetProfile
from src.sources.sec import SecEdgarClient, SubmissionFilingEntry

logger = logging.getLogger(__name__)

_ITEM_CODE_RE = re.compile(r"(?P<major>\d{1,2})\s*\.\s*(?P<minor>\d{2})")
_CURRENT_REPORT_ITEM_HEADING_RE = re.compile(
    r"(?im)^[ \t>#*_\-]*(?:item\s+)?(?P<item>\d{1,2}\s*\.\s*\d{2})"
    r"(?:[.\-:)\u2013\u2014]\s*)?(?P<title>[^\n]{0,180})$"
)


class SecTransactionSignalSource:
    """Build SEC 8-K transaction-signal documents from lower-level EDGAR primitives."""

    def __init__(self, sec_client: SecEdgarClient) -> None:
        self.sec_client = sec_client

    def fetch_transaction_signal_documents(
        self,
        target_profile: TargetProfile,
        since: date,
        ttl_hours: int,
        form_type: str,
        limit: int,
        company_limit: int,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
        primary_items: list[str] | None = None,
        supporting_items: list[str] | None = None,
        require_primary_item: bool = False,
        fetch_filing_text: bool = False,
        text_scope: str | None = None,
        as_of_date: date | None = None,
    ) -> list[SourceDocument]:
        """Return same-industry SEC Item filings for acquisition-history recall."""

        target_sic = _normalize_sic(target_profile.sic)
        if not target_sic:
            return []

        normalized_primary_items = _normalize_item_codes(primary_items or [])
        normalized_supporting_items = _normalize_item_codes(supporting_items or [])
        filing_end_date = as_of_date or datetime.now(UTC).date()
        candidate_documents = self.sec_client.list_public_company_profiles_by_sic(
            target_sic,
            limit=company_limit,
            ttl_hours=ttl_hours,
            source_strength=source_strength,
            source_dimension=source_dimension,
        )
        documents: list[SourceDocument] = []
        for candidate_document in candidate_documents:
            candidate_target = _resolved_target_from_company_document(candidate_document, target_sic)
            if not candidate_target or _same_company(candidate_target, target_profile):
                continue

            try:
                filing_entries = self.sec_client.list_submission_filing_entries(
                    candidate_target,
                    since,
                    filing_end_date,
                    form_type,
                    required_items=normalized_primary_items,
                    supporting_items=normalized_supporting_items,
                    require_required_item=require_primary_item,
                )
            except Exception as error:
                logger.warning("SEC transaction submissions lookup failed: cik=%s error=%s", candidate_target.cik, error)
                continue

            for entry in filing_entries:
                documents.append(
                    self._transaction_source_document_for_filing(
                        candidate_target,
                        entry,
                        ttl_hours,
                        source_strength,
                        source_dimension,
                        fetch_filing_text,
                        text_scope,
                        normalized_primary_items,
                    )
                )
                if len(documents) >= limit:
                    return documents
        return documents

    def _transaction_source_document_for_filing(
        self,
        target: ResolvedTarget,
        entry: SubmissionFilingEntry,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
        fetch_filing_text: bool,
        text_scope: str | None,
        primary_items: list[str],
    ) -> SourceDocument:
        filing = entry.filing
        metadata = _transaction_metadata(entry.metadata)
        raw_text = _item_signal_summary_text(metadata)
        if fetch_filing_text:
            archive_text = self.sec_client.fetch_archive_filing_text(
                metadata.get("primary_document_url"),
                target,
                source_dimension,
            )
            if archive_text:
                selected_text, selected_scope = _select_8k_transaction_text(archive_text, primary_items, text_scope)
                raw_text = selected_text or raw_text
                metadata = {
                    **metadata,
                    "text_retrieval_method": "sec_archive_primary_document",
                    "text_scope": selected_scope,
                    "raw_text_char_count": len(archive_text),
                    "cached_text_char_count": len(raw_text or ""),
                }
            else:
                metadata = {
                    **metadata,
                    "text_retrieval_method": "sec_archive_primary_document",
                    "text_retrieval_error": "primary document text unavailable",
                }

        return SourceDocument(
            source_id="edgar",
            source_dimension=source_dimension,
            source_type=SourceType.sec_filing,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=filing.url,
            filing_accession=filing.accession_number,
            raw_text=raw_text,
            metadata=metadata,
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )


def _transaction_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    item_match = dict(metadata.get("edgar_item_match") or {})
    primary_items = [str(item) for item in item_match.get("required", [])]
    item_match["primary"] = primary_items
    item_match["required_primary_items"] = item_match.get("configured_required_items", [])
    return {
        **metadata,
        "edgar_item_match": item_match,
        "provider_method": "sec_transaction_signal_same_sic_item_filter",
        "industry_match_basis": "same_sic_filer",
    }


def _resolved_target_from_company_document(document: SourceDocument, fallback_sic: str) -> ResolvedTarget | None:
    cik = _normalize_cik(document.metadata.get("cik") or document.target_cik)
    name = document.metadata.get("canonical_name") or document.metadata.get("name") or document.metadata.get("company")
    ticker = document.metadata.get("ticker") or document.target_ticker or cik
    if not cik or not name or not ticker:
        return None
    return ResolvedTarget(
        canonical_name=str(name),
        ticker=str(ticker),
        cik=cik,
        exchange=str(document.metadata.get("exchange")) if document.metadata.get("exchange") else None,
        sic=_normalize_sic(document.metadata.get("sic")) or fallback_sic,
        resolution_confidence=0.0,
        matched_input=str(ticker),
        source_provenance=["edgar:same_sic_company_profile"],
    )


def _same_company(candidate: ResolvedTarget, target_profile: TargetProfile) -> bool:
    target_cik = _normalize_cik(target_profile.cik)
    if target_cik and candidate.cik == target_cik:
        return True
    target_ticker = str(target_profile.ticker or "").strip().casefold()
    return bool(target_ticker and candidate.ticker.strip().casefold() == target_ticker)


def _item_signal_summary_text(metadata: dict[str, Any]) -> str:
    item_match = metadata.get("edgar_item_match")
    primary_items: list[str] = []
    supporting_items: list[str] = []
    if isinstance(item_match, dict):
        primary_items = [str(item) for item in item_match.get("primary", [])]
        supporting_items = [str(item) for item in item_match.get("supporting", [])]
    primary_text = ", ".join(primary_items) if primary_items else "configured current-report item"
    supporting_text = f"; supporting Item(s) {', '.join(supporting_items)}" if supporting_items else ""
    return (
        f"{metadata.get('canonical_name')} filed an 8-K Item {primary_text}{supporting_text}. "
        "Item 2.01 is Completion of Acquisition or Disposition of Assets."
    )


def _select_8k_transaction_text(
    filing_text: str,
    primary_items: list[str],
    text_scope: str | None,
    max_chars: int = 15000,
) -> tuple[str | None, str]:
    if text_scope and text_scope != "primary_item_section":
        return filing_text[:max_chars], text_scope

    selected_sections: list[str] = []
    for item_code in primary_items:
        section = _extract_item_section(filing_text, item_code)
        if section:
            selected_sections.append(section)
    if selected_sections:
        return "\n\n".join(selected_sections)[:max_chars], "primary_item_section"
    return filing_text[:max_chars], "full_8k_current_report"


def _extract_item_section(text: str, item_code: str) -> str | None:
    headings = list(_CURRENT_REPORT_ITEM_HEADING_RE.finditer(text))
    if not headings:
        return None

    normalized_item = _normalize_item_code(item_code)
    for index, heading in enumerate(headings):
        heading_item = _normalize_item_code(heading.group("item"))
        if heading_item != normalized_item:
            continue
        next_heading = headings[index + 1] if index + 1 < len(headings) else None
        end = next_heading.start() if next_heading else len(text)
        section = text[heading.start() : end].strip()
        return section or None
    return None


def _normalize_item_codes(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _normalize_item_code(value)
        if item and item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def _normalize_item_code(value: str) -> str | None:
    match = _ITEM_CODE_RE.search(value)
    if not match:
        return None
    return f"{int(match.group('major'))}.{match.group('minor')}"


def _normalize_sic(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits or text


def _normalize_cik(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.zfill(10) if text.isdigit() else text
