"""Target-profile SEC filing text selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain import FilingMetadata, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.sources.sec import FilingTextExtraction


def source_document_for_filing_text_scope(
    target: ResolvedTarget,
    filing: FilingMetadata,
    extraction: FilingTextExtraction,
    ttl_hours: int,
    *,
    source_strength: SourceStrength,
    source_dimension: str | None,
    text_scope: str,
) -> SourceDocument:
    """Build a target-profile text document from provider-level SEC filing sections."""

    selected_text, selected_scope = _select_profile_section(extraction.structured_sections, filing, text_scope)
    if not selected_text and extraction.text_scope != "structured_sections":
        selected_text = extraction.selected_text
        selected_scope = extraction.text_scope
    if not selected_text:
        raise ValueError(f"Could not retrieve structured filing section for accession {filing.accession_number}")

    retrieved_at = datetime.now(UTC)
    metadata = {
        **filing.model_dump(mode="json"),
        "text_retrieval_method": extraction.retrieval_method,
        "text_scope": selected_scope,
        "raw_text_char_count": extraction.raw_text_char_count,
        "cached_text_char_count": len(selected_text),
    }
    if extraction.structured_sections:
        metadata["structured_sections"] = extraction.structured_sections
        metadata["structured_section_char_counts"] = {
            section_name: len(section_text) for section_name, section_text in extraction.structured_sections.items()
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
        raw_text=selected_text,
        metadata=metadata,
        retrieved_at=retrieved_at,
        expires_at=retrieved_at + timedelta(hours=ttl_hours) if ttl_hours else None,
    )


def _select_profile_section(
    sections: dict[str, str],
    filing: FilingMetadata,
    text_scope: str,
) -> tuple[str | None, str]:
    normalized_form = filing.form.strip().upper()
    if text_scope == "company_strategy":
        if normalized_form.startswith("10-K") and sections.get("management_discussion"):
            return sections["management_discussion"], "item_7_mdna"
        if normalized_form.startswith("10-Q") and sections.get("management_discussion"):
            return sections["management_discussion"], "item_2_mdna"
        if normalized_form.startswith("8-K") and sections.get("current_report"):
            return sections["current_report"], "full_8k_current_report"
        if normalized_form.startswith("S-1") and sections.get("registration_statement"):
            return sections["registration_statement"], "full_s1_registration_statement"
        return None, _full_text_scope_for_form(filing.form)

    if sections.get("business"):
        return sections["business"], "item_1_business"
    return None, "full_filing_text"


def _full_text_scope_for_form(form: str) -> str:
    normalized_form = form.strip().upper()
    if normalized_form.startswith("8-K"):
        return "full_8k_current_report"
    if normalized_form.startswith("S-1"):
        return "full_s1_registration_statement"
    return "full_filing_text"
