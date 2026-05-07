"""Shared helpers for strategic buyer retrievers."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from src.domain import RetrievalRetrieverConfig, SourceDocument, SourceStrength, SourceType, TargetProfile
from src.sources.strategy import DataSourceStrategy, SelectedDataSource


def selected_source(
    strategy: DataSourceStrategy,
    use_case: str,
    source_id: str,
    retriever_name: str,
    warnings: list[str],
    *,
    required: bool = True,
) -> SelectedDataSource | None:
    selected = strategy.selected_source(use_case, source_id, include_disabled=True)
    if not selected:
        if required:
            warnings.append(f"{retriever_name} skipped {source_id}: no retrieval rules for {use_case}")
        return None
    if not selected.enabled:
        warnings.append(f"{retriever_name} {source_id} disabled: {selected.disabled_reason}")
        return None
    return selected


def retriever_config(
    strategy: DataSourceStrategy,
    retriever_name: str,
    warnings: list[str],
) -> RetrievalRetrieverConfig | None:
    config = strategy.retriever_config(retriever_name)
    if config is None:
        warnings.append(f"{retriever_name} skipped: no retriever rules in retrieval_rules.yaml")
    return config


def required_source_role(
    strategy: DataSourceStrategy,
    use_case: str,
    role: str,
    retriever_name: str,
    warnings: list[str],
) -> str | None:
    source = strategy.selected_source_by_role(use_case, role, include_disabled=True)
    if not source:
        warnings.append(f"{retriever_name} skipped: retriever policy has no source role {role}")
        return None
    return source.source_id


def optional_source_role(strategy: DataSourceStrategy, use_case: str, role: str) -> str | None:
    source = strategy.selected_source_by_role(use_case, role, include_disabled=True)
    return source.source_id if source else None


def required_positive_int(
    config: RetrievalRetrieverConfig,
    field_name: str,
    retriever_name: str,
    warnings: list[str],
) -> int | None:
    value = getattr(config, field_name)
    if value is None:
        warnings.append(f"{retriever_name} skipped: retriever policy has no {field_name}")
    return value


def source_role_map(strategy: DataSourceStrategy, use_case: str) -> dict[str, str]:
    return strategy.source_role_map(use_case, include_disabled=True)


def source_execution_order(strategy: DataSourceStrategy, use_case: str) -> list[str]:
    return unique_terms([source.source_id for source in strategy.selected_sources_by_priority(use_case, include_disabled=True)])


def document_matches_sic(document: SourceDocument, target_sic: str) -> bool:
    metadata = document.metadata
    candidate_sic = normalize_sic(metadata.get("sic") or metadata.get("sic_code") or metadata.get("SIC"))
    return candidate_sic == target_sic


def has_evidence_reference(document: SourceDocument) -> bool:
    return bool(document.url or document.filing_accession)


def is_self_candidate(metadata: dict[str, Any], target_profile: TargetProfile, candidate_name: str) -> bool:
    candidate_cik = normalize_cik(metadata.get("cik") or metadata.get("candidate_cik"))
    if candidate_cik and normalize_cik(target_profile.cik) == candidate_cik:
        return True
    candidate_ticker = first_text(metadata, "ticker", "candidate_ticker")
    if candidate_ticker and candidate_ticker.casefold() == target_profile.ticker.casefold():
        return True
    return normalize_entity_key(candidate_name) == normalize_entity_key(target_profile.name)


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def calendar_years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        # Leap-day runs use Feb. 28 in the target year to keep the window calendar-based.
        return value.replace(year=value.year - years, day=28)


def first_text(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def unique_terms(values: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = re.sub(r"\s+", " ", str(value)).strip()
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def normalize_sic(value: Any) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value).strip())
    return digits or None


def normalize_cik(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.zfill(10) if text.isdigit() else text


def normalize_entity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def source_type_value(source_type: SourceType | str) -> str:
    return source_type.value if isinstance(source_type, SourceType) else str(source_type)


def source_label(source_id: str, form_type: str | None = None) -> str:
    parts = [source_id]
    if form_type:
        parts.append(re.sub(r"[^a-z0-9]+", "", form_type.casefold()))
    return "_".join(part for part in parts if part)


def confidence_from_strength(source_strength: SourceStrength) -> float:
    return {
        SourceStrength.A: 0.82,
        SourceStrength.B: 0.72,
        SourceStrength.C: 0.55,
        SourceStrength.D: 0.35,
    }[source_strength]
