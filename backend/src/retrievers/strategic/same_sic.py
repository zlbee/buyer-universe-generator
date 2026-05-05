"""Same-SIC strategic buyer retriever."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from src.domain import BuyerType, CandidateHit, Evidence, SourceDocument, StrategicRetrievalResult, TargetProfile
from src.retrievers.strategic.common import (
    confidence_from_strength,
    document_matches_sic,
    first_text,
    has_evidence_reference,
    is_self_candidate,
    normalize_cik,
    normalize_sic,
    required_positive_int,
    required_source_role,
    retriever_config,
    selected_source,
    source_type_value,
)
from src.retrievers.strategic.protocols import PublicCompanySicSource
from src.sources.strategy import DataSourceStrategy


logger = logging.getLogger(__name__)


class SameSicRetriever:
    """Recalls public strategic buyers whose public-company SIC matches the target."""

    name = "SameSicRetriever"
    source_path = "same_sic"

    def __init__(
        self,
        strategy: DataSourceStrategy,
        edgar_client: PublicCompanySicSource | None,
    ) -> None:
        self.strategy = strategy
        self.edgar_client = edgar_client

    def retrieve(self, target_profile: TargetProfile) -> list[CandidateHit]:
        return self.retrieve_with_context(target_profile).hits

    def retrieve_with_context(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        warnings: list[str] = []
        config = retriever_config(self.strategy, self.name, warnings)
        if not config:
            logger.warning("SameSicRetriever skipped: no retriever policy target=%s", target_profile.ticker)
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        max_candidates = required_positive_int(config, "max_candidates", self.name, warnings)
        edgar_source_id = required_source_role(config, "public_company_metadata", self.name, warnings)
        if max_candidates is None or not edgar_source_id:
            logger.warning(
                "SameSicRetriever skipped: incomplete retriever policy target=%s max_candidates=%s edgar_source_id=%s",
                target_profile.ticker,
                max_candidates,
                edgar_source_id,
            )
            return StrategicRetrievalResult(warnings=warnings, metadata={"retriever": self.name})

        target_sic = normalize_sic(target_profile.sic)
        metadata: dict[str, Any] = {
            "retriever": self.name,
            "source_use_case": config.use_case,
            "source_roles": config.source_roles,
            "target_sic": target_sic,
        }
        if not target_sic:
            logger.warning("SameSicRetriever skipped: target has no SIC target=%s name=%s", target_profile.ticker, target_profile.name)
            return StrategicRetrievalResult(
                warnings=[f"{self.name} skipped: target profile has no SIC"],
                metadata=metadata,
            )

        logger.info(
            "SameSicRetriever started: target=%s cik=%s sic=%s use_case=%s max_candidates=%s source_roles=%s",
            target_profile.ticker,
            target_profile.cik,
            target_sic,
            config.use_case,
            max_candidates,
            config.source_roles,
        )

        edgar_source = selected_source(self.strategy, config.use_case, edgar_source_id, self.name, warnings)
        if not edgar_source:
            logger.warning(
                "SameSicRetriever skipped: EDGAR source unavailable target=%s sic=%s warnings=%s",
                target_profile.ticker,
                target_sic,
                warnings,
            )
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)
        if not self.edgar_client:
            warnings.append(f"{self.name} skipped: EDGAR adapter unavailable")
            logger.warning("SameSicRetriever skipped: EDGAR adapter unavailable target=%s sic=%s", target_profile.ticker, target_sic)
            return StrategicRetrievalResult(warnings=warnings, metadata=metadata)

        logger.info(
            "SameSicRetriever querying public companies by SIC: target=%s sic=%s source=%s dimension=%s limit=%s",
            target_profile.ticker,
            target_sic,
            edgar_source_id,
            edgar_source.dimension_id,
            max_candidates + 1,
        )
        documents = self.edgar_client.list_public_company_profiles_by_sic(
            target_sic,
            limit=max_candidates + 1,
            ttl_hours=self.strategy.cache_ttl_hours(edgar_source_id),
            source_strength=edgar_source.source_strength,
            source_dimension=edgar_source.dimension_id,
        )

        logger.info(
            "SameSicRetriever fetched candidate documents: target=%s sic=%s document_count=%s",
            target_profile.ticker,
            target_sic,
            len(documents),
        )
        for index, document in enumerate(documents[:5], start=1):
            logger.info(
                "SameSicRetriever document sample: target=%s index=%s name=%s cik=%s sic=%s source_type=%s url_present=%s",
                target_profile.ticker,
                index,
                first_text(document.metadata, "canonical_name", "candidate_name", "name", "company"),
                normalize_cik(document.metadata.get("cik") or document.metadata.get("candidate_cik") or document.target_cik),
                normalize_sic(document.metadata.get("sic") or document.metadata.get("sic_code") or document.metadata.get("SIC")),
                source_type_value(document.source_type),
                bool(document.url or document.filing_accession),
            )

        hits: list[CandidateHit] = []
        skipped_reasons: dict[str, int] = defaultdict(int)
        for document in documents:
            hit = _same_sic_hit_from_document(document, target_profile, target_sic, self.name, self.source_path)
            if not hit:
                skipped_reasons[_same_sic_skip_reason(document, target_profile, target_sic)] += 1
                continue
            hits.append(hit)
            if len(hits) >= max_candidates:
                break

        skipped_without_evidence = skipped_reasons.get("missing_evidence", 0)
        if skipped_without_evidence:
            warnings.append(f"{self.name} skipped {skipped_without_evidence} same-SIC candidates without traceable evidence")
        metadata["candidate_documents_checked"] = len(documents)
        metadata["hit_count"] = len(hits)
        metadata["skip_reasons"] = dict(skipped_reasons)
        logger.info(
            "SameSicRetriever completed: target=%s sic=%s hits=%s documents_checked=%s skip_reasons=%s warnings=%s",
            target_profile.ticker,
            target_sic,
            len(hits),
            len(documents),
            dict(skipped_reasons),
            warnings,
        )
        return StrategicRetrievalResult(hits=hits, warnings=warnings, metadata=metadata)


def _same_sic_hit_from_document(
    document: SourceDocument,
    target_profile: TargetProfile,
    target_sic: str,
    retriever_name: str,
    source_path: str,
) -> CandidateHit | None:
    if not document_matches_sic(document, target_sic):
        return None

    metadata = document.metadata
    candidate_name = first_text(metadata, "canonical_name", "candidate_name", "name", "company")
    if not candidate_name or is_self_candidate(metadata, target_profile, candidate_name):
        return None
    if not has_evidence_reference(document):
        return None

    candidate_sic = normalize_sic(metadata.get("sic") or metadata.get("sic_code") or metadata.get("SIC"))
    claim = f"{candidate_name} has public-company SIC {candidate_sic}, matching target SIC {target_sic}."
    evidence = Evidence(
        claim=claim,
        source_type=source_type_value(document.source_type),
        source_dimension=document.source_dimension,
        source_strength=document.source_strength,
        url=document.url,
        filing_accession=document.filing_accession,
        quote_or_snippet=f"SIC {candidate_sic}",
        verified_fact=True,
    )
    ticker = first_text(metadata, "ticker", "candidate_ticker")
    cik = normalize_cik(metadata.get("cik") or metadata.get("candidate_cik") or document.target_cik)
    return CandidateHit(
        candidate_name=candidate_name,
        candidate_ticker=ticker,
        candidate_cik=cik,
        candidate_domain=first_text(metadata, "domain", "homepage_url", "website"),
        buyer_type=BuyerType.strategic,
        retriever_name=retriever_name,
        source_path=[source_path],
        data_source=[document.source_id],
        fit_reason=f"Shares target SIC {target_sic}.",
        evidence=[evidence],
        confidence=confidence_from_strength(document.source_strength),
        retrieval_metadata={
            "target_sic": target_sic,
            "candidate_sic": candidate_sic,
            "exchange": metadata.get("exchange"),
            "active_status": metadata.get("active_status") or metadata.get("active"),
            "market_cap": metadata.get("market_cap") or metadata.get("market_cap_usd"),
        },
    )


def _same_sic_skip_reason(document: SourceDocument, target_profile: TargetProfile, target_sic: str) -> str:
    if not document_matches_sic(document, target_sic):
        return "sic_mismatch"

    metadata = document.metadata
    candidate_name = first_text(metadata, "canonical_name", "candidate_name", "name", "company")
    if not candidate_name:
        return "missing_name"
    if is_self_candidate(metadata, target_profile, candidate_name):
        return "self_candidate"
    if not has_evidence_reference(document):
        return "missing_evidence"
    return "unknown"
