"""Investor relations page discovery and validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.config import Settings
from src.domain import DataSourceRawRecord, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.llm import LLMResponseError, MissingLLMConfigurationError, WebSearchJSONClient
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient

logger = logging.getLogger(__name__)

_IR_DISCOVERY_LLM_BUSINESS_TYPE = "investor_relations_page_discovery"


class IRPageCandidateOutput(BaseModel):
    """Candidate investor relations page returned by the LLM web-search step."""

    model_config = ConfigDict(extra="ignore")

    url: str
    title: str | None = None
    snippet: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None
    source: str | None = None

    @field_validator("url")
    @classmethod
    def url_must_not_be_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("candidate url must not be empty")
        return normalized


class IRPageDiscoveryOutput(BaseModel):
    """Expected JSON object returned by the IR discovery prompt."""

    model_config = ConfigDict(extra="ignore")

    candidates: list[IRPageCandidateOutput] = Field(default_factory=list)


@dataclass
class IRPageDiscoveryResult:
    document: SourceDocument | None
    status: str
    selected_url: str | None = None
    candidates_checked: int = 0
    rejection_reasons: list[str] | None = None


@dataclass(frozen=True)
class _CandidateValidation:
    accepted: bool
    reason: str
    document: SourceDocument | None = None
    validated_signals: list[str] | None = None


class InvestorRelationsPageDiscovery(ExternalDataSourceClient):
    """Finds and validates official investor relations pages using LLM web search."""

    schema_name = "InvestorRelationsPageDiscovery"

    def __init__(
        self,
        settings: Settings,
        web_search_client: WebSearchJSONClient,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "official_investor_relations_pages",
    ) -> None:
        super().__init__(
            settings,
            source_id="company_pages",
            provider=provider,
            http_client=http_client or httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True),
            request_recorder=request_recorder,
        )
        self.web_search_client = web_search_client

    def discover(
        self,
        target: ResolvedTarget,
        homepage_url: str | None,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> IRPageDiscoveryResult:
        prompt = _build_ir_discovery_prompt(target, homepage_url, self.settings.ir_discovery_max_candidates)
        try:
            payload = self.web_search_client.generate_json_with_web_search(
                prompt,
                self.schema_name,
                ir_discovery_json_schema(),
                system_prompt=ir_discovery_system_prompt(self.schema_name),
                max_results=self.settings.ir_discovery_max_candidates,
                max_total_results=self.settings.ir_discovery_max_candidates,
                search_engine=self.settings.ir_discovery_search_engine,
                search_context_size=self.settings.ir_discovery_search_context_size,
                source_business_type=_IR_DISCOVERY_LLM_BUSINESS_TYPE,
            )
            discovery_output = IRPageDiscoveryOutput.model_validate(payload)
        except MissingLLMConfigurationError:
            raise
        except (LLMResponseError, ValidationError) as error:
            logger.warning(
                "IR page discovery LLM step failed: ticker=%s error_type=%s error=%s",
                target.ticker,
                type(error).__name__,
                error,
            )
            return IRPageDiscoveryResult(
                document=None,
                status="llm_error",
                rejection_reasons=[f"{type(error).__name__}: {error}"],
            )

        candidates = _dedupe_candidates(discovery_output.candidates)[: self.settings.ir_discovery_max_candidates]
        if not candidates:
            return IRPageDiscoveryResult(document=None, status="no_candidates", rejection_reasons=["LLM returned no IR candidates"])

        rejection_reasons: list[str] = []
        for rank, candidate in enumerate(candidates, start=1):
            validation = self._validate_candidate(
                target,
                homepage_url,
                candidate,
                rank,
                ttl_hours,
                source_strength,
                source_dimension,
            )
            if validation.accepted and validation.document:
                logger.info(
                    "IR page discovery selected page: ticker=%s url=%s rank=%s signals=%s",
                    target.ticker,
                    validation.document.url,
                    rank,
                    validation.validated_signals,
                )
                return IRPageDiscoveryResult(
                    document=validation.document,
                    status="selected",
                    selected_url=validation.document.url,
                    candidates_checked=rank,
                    rejection_reasons=rejection_reasons,
                )
            rejection_reasons.append(f"{candidate.url}: {validation.reason}")

        return IRPageDiscoveryResult(
            document=None,
            status="no_valid_candidate",
            candidates_checked=len(candidates),
            rejection_reasons=rejection_reasons,
        )

    def _validate_candidate(
        self,
        target: ResolvedTarget,
        homepage_url: str | None,
        candidate: IRPageCandidateOutput,
        rank: int,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
    ) -> _CandidateValidation:
        candidate_url = _normalize_url(candidate.url)
        if not candidate_url:
            return _CandidateValidation(False, "candidate URL is not http(s)")

        try:
            html, raw_records = self._get_text(
                operation="ir_candidate_validation",
                url=candidate_url,
                headers={"User-Agent": self.settings.sec_user_agent},
                target=target,
                source_dimension=source_dimension,
                raw_records_from_text=lambda value, context, retrieved_at, response: [
                    _ir_candidate_raw_record(
                        value,
                        context,
                        retrieved_at,
                        response,
                        candidate=candidate,
                        rank=rank,
                        homepage_url=homepage_url,
                    )
                ],
            )
        except httpx.HTTPStatusError as error:
            return _CandidateValidation(False, f"HTTP {error.response.status_code}")
        except httpx.HTTPError as error:
            return _CandidateValidation(False, f"HTTP error: {type(error).__name__}")

        raw_text = _clean_page_text(html)
        if not raw_text:
            return _CandidateValidation(False, "page text is empty")

        raw_record = raw_records[0] if raw_records else None
        final_url = raw_record.url if raw_record and raw_record.url else candidate_url
        title = _extract_title(html)
        high_context = " ".join(
            value
            for value in (candidate.title, candidate.snippet, final_url, title)
            if isinstance(value, str) and value.strip()
        )
        combined_context = f"{high_context} {raw_text[:8000]}"

        ir_signals = _matched_investor_signals(combined_context)
        high_context_ir_signals = _matched_investor_signals(high_context)
        host_has_ir_signal = _host_has_ir_signal(final_url)
        if not high_context_ir_signals and not host_has_ir_signal and len(ir_signals) < 2:
            return _CandidateValidation(False, "missing investor-relations page signals")
        if _is_root_homepage(final_url) and not host_has_ir_signal and not high_context_ir_signals:
            return _CandidateValidation(False, "root homepage is not an investor-relations page")

        storefront_signals = _matched_storefront_signals(combined_context)
        if storefront_signals and not host_has_ir_signal and "investor relations" not in high_context.casefold():
            return _CandidateValidation(False, f"storefront/homepage signals dominate: {', '.join(storefront_signals[:3])}")

        company_signals = _matched_company_identity_signals(target, homepage_url, final_url, combined_context)
        if not company_signals:
            return _CandidateValidation(False, "candidate does not match company identity")

        validated_signals = _dedupe_strings([*ir_signals, *company_signals])
        retrieved_at = raw_record.retrieved_at if raw_record else datetime.now(UTC)
        document = SourceDocument(
            source_id="company_pages",
            source_dimension=source_dimension,
            source_type=SourceType.company_page,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=final_url,
            raw_text=raw_text,
            metadata={
                "page_role": "investor_relations",
                "discovered_from": "llm_web_search",
                "discovery_provider": "openrouter.ai",
                "domain": urlparse(final_url).netloc,
                "source_candidate_url": candidate_url,
                "candidate_rank": rank,
                "candidate_title": candidate.title,
                "candidate_snippet": candidate.snippet,
                "candidate_confidence": candidate.confidence,
                "candidate_reason": candidate.reason,
                "candidate_source": candidate.source,
                "homepage_context_url": homepage_url,
                "validated_signals": validated_signals,
            },
            retrieved_at=retrieved_at,
            expires_at=retrieved_at + timedelta(hours=ttl_hours) if ttl_hours else None,
        )
        return _CandidateValidation(True, "accepted", document=document, validated_signals=validated_signals)


def _ir_candidate_raw_record(
    html: str,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    response: httpx.Response,
    *,
    candidate: IRPageCandidateOutput,
    rank: int,
    homepage_url: str | None,
) -> DataSourceRawRecord:
    final_url = str(response.url)
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=SourceType.company_page,
        target_cik=context.target_cik,
        target_ticker=context.target_ticker,
        record_id=final_url,
        url=final_url,
        raw_payload={
            "url": final_url,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
        },
        raw_text=html,
        metadata={
            "page_role": "investor_relations_candidate",
            "discovered_from": "llm_web_search",
            "domain": urlparse(final_url).netloc,
            "source_candidate_url": candidate.url,
            "candidate_rank": rank,
            "candidate_title": candidate.title,
            "candidate_snippet": candidate.snippet,
            "candidate_confidence": candidate.confidence,
            "candidate_reason": candidate.reason,
            "candidate_source": candidate.source,
            "homepage_context_url": homepage_url,
        },
        retrieved_at=retrieved_at,
    )


def ir_discovery_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "title", "snippet", "confidence", "reason", "source"],
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "snippet": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "confidence": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        },
    }


def ir_discovery_system_prompt(schema_name: str) -> str:
    return f"Return only valid JSON for schema {schema_name}; do not include prose."


def _build_ir_discovery_prompt(target: ResolvedTarget, homepage_url: str | None, max_candidates: int) -> str:
    schema = json.dumps(ir_discovery_json_schema(), sort_keys=True)
    return f"""
Find the official investor relations website for this public company.

Company:
- canonical_name: {target.canonical_name}
- ticker: {target.ticker}
- cik: {target.cik}
- exchange: {target.exchange}
- sic: {target.sic}
- known_homepage: {homepage_url or "unknown"}

Use web search. Prefer pages titled Investor Relations, Investors, SEC Filings, Financials, Stock Information, or similar.
Do not return the commercial storefront homepage unless it is itself an investor relations page.
Return up to {max_candidates} candidates ordered from most likely to least likely.

Return JSON matching this schema:
{schema}
""".strip()


def _dedupe_candidates(candidates: list[IRPageCandidateOutput]) -> list[IRPageCandidateOutput]:
    seen: set[str] = set()
    deduped: list[IRPageCandidateOutput] = []
    for candidate in candidates:
        normalized_url = _normalize_url(candidate.url)
        if not normalized_url or normalized_url.casefold() in seen:
            continue
        seen.add(normalized_url.casefold())
        deduped.append(candidate.model_copy(update={"url": normalized_url}))
    return deduped


def _normalize_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip().split("#", 1)[0]


def _clean_page_text(value: str) -> str:
    without_scripts = value
    for start_tag, end_tag in (("<script", "</script>"), ("<style", "</style>")):
        while True:
            start = without_scripts.lower().find(start_tag)
            if start < 0:
                break
            end = without_scripts.lower().find(end_tag, start)
            if end < 0:
                without_scripts = without_scripts[:start]
                break
            without_scripts = without_scripts[:start] + " " + without_scripts[end + len(end_tag) :]

    parser = _TextParser()
    parser.feed(without_scripts)
    return " ".join(" ".join(parser.text_parts).split())[:20_000]


def _extract_title(value: str) -> str | None:
    parser = _TitleParser()
    parser.feed(value)
    title = " ".join("".join(parser.title_parts).split())
    return title or None


def _matched_investor_signals(value: str) -> list[str]:
    normalized = _normalize_text(value)
    signals = (
        "investor relations",
        "investors",
        "sec filings",
        "financials",
        "stock information",
        "annual reports",
        "quarterly results",
        "events & presentations",
        "events and presentations",
        "shareholder",
        "earnings",
    )
    return [signal for signal in signals if signal in normalized]


def _matched_storefront_signals(value: str) -> list[str]:
    normalized = _normalize_text(value)
    signals = (
        "afterpay",
        "pay in 4",
        "interest free payments",
        "shop now",
        "free shipping",
        "add to cart",
        "checkout",
        "promo code",
    )
    return [signal for signal in signals if signal in normalized]


def _host_has_ir_signal(value: str) -> bool:
    host = urlparse(value).netloc.casefold()
    return host.startswith("investor.") or host.startswith("investors.") or ".ir." in host or host.startswith("ir.")


def _is_root_homepage(value: str) -> bool:
    path = urlparse(value).path.strip("/")
    return not path


def _matched_company_identity_signals(
    target: ResolvedTarget,
    homepage_url: str | None,
    candidate_url: str,
    context: str,
) -> list[str]:
    signals: list[str] = []
    homepage_domain = _registrable_domain(homepage_url) if homepage_url else None
    candidate_domain = _registrable_domain(candidate_url)
    if homepage_domain and candidate_domain and homepage_domain == candidate_domain:
        signals.append("same_registrable_domain")

    host_compact = re.sub(r"[^a-z0-9]", "", urlparse(candidate_url).netloc.casefold())
    context_normalized = _normalize_text(context)
    context_compact = re.sub(r"[^a-z0-9]", "", context.casefold())
    for variant in _company_compact_variants(target.canonical_name):
        if variant in host_compact or variant in context_compact:
            signals.append("company_name_compact_match")
            break

    tokens = _company_identity_tokens(target.canonical_name)
    required_matches = 1 if len(tokens) <= 1 else 2
    token_matches = [token for token in tokens if token in context_normalized]
    if len(token_matches) >= required_matches:
        signals.append("company_name_token_match")

    if target.ticker and re.search(rf"\b{re.escape(target.ticker.casefold())}\b", context_normalized):
        signals.append("ticker_match")

    return _dedupe_strings(signals)


def _registrable_domain(value: str | None) -> str | None:
    if not value:
        return None
    host = urlparse(value).netloc.casefold()
    labels = [label for label in host.split(".") if label and label != "www"]
    if len(labels) < 2:
        return host or None
    return ".".join(labels[-2:])


def _company_compact_variants(name: str) -> list[str]:
    words = _company_words(name, include_short=True)
    variants = ["".join(words)]
    if words and words[0] == "the":
        variants.append("".join(words[1:]))
    return [variant for variant in variants if len(variant) >= 4]


def _company_identity_tokens(name: str) -> list[str]:
    return [word for word in _company_words(name, include_short=False) if len(word) >= 4]


def _company_words(name: str, include_short: bool) -> list[str]:
    suffixes = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "plc",
        "holdings",
        "holding",
        "class",
        "common",
        "stock",
    }
    words = re.findall(r"[a-z0-9]+", name.casefold())
    return [word for word in words if word not in suffixes and (include_short or len(word) > 1)]


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("-", " ").replace("_", " ").split()).casefold()


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


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_parts.append(data)


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "title":
            self.in_title = True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False
