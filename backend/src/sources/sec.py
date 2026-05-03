"""SEC/EDGAR source adapter for Phase 1 target resolution and filing metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import Settings
from src.domain import (
    DataSourceRawRecord,
    DataSourceRequestStatus,
    FilingMetadata,
    ResolvedTarget,
    SourceDocument,
    SourceStrength,
    SourceType,
)
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient


@dataclass(frozen=True)
class FilingTextExtraction:
    """Selected SEC filing text plus provenance about how it was extracted."""

    selected_text: str
    retrieval_method: str
    text_scope: str
    raw_text_char_count: int
    cached_text_char_count: int
    structured_sections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MarkdownItemSection:
    """A deterministic SEC Item section extracted from edgartools markdown."""

    text: str
    section_name: str
    selected_scope: str
    start_item: str
    end_item: str


@dataclass(frozen=True)
class MarkdownItemHeading:
    """Line-level SEC Item heading found in markdown output."""

    item_code: str
    start: int
    end: int


_ITEM_HEADING_RE = re.compile(
    r"^[ \t>#*_\-]*(?:part\s+[ivxlcdm]+\s+)?item\s+"
    r"(?P<item>\d{1,2}\s*[A-Z]?)\s*"
    r"(?:[.\-:)\u2013\u2014]\s*)?"
    r"(?P<title>[^\n]{0,160})$",
    re.IGNORECASE | re.MULTILINE,
)


class SecEdgarClient(ExternalDataSourceClient):
    """Reads SEC company mapping and submissions metadata for US-listed targets."""

    company_tickers_exchange_url = "https://www.sec.gov/files/company_tickers_exchange.json"
    submissions_url_template = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        edgar_module: Any | None = None,
        use_edgartools: bool = True,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "edgartools",
        markdown_item_parser_enabled: bool = False,
        markdown_item_parser_min_chars: int = 500,
    ) -> None:
        super().__init__(
            settings,
            source_id="edgar",
            provider=provider,
            http_client=http_client,
            request_recorder=request_recorder,
        )
        self._edgar_module = edgar_module
        self._use_edgartools = use_edgartools
        self.markdown_item_parser_enabled = markdown_item_parser_enabled
        self.markdown_item_parser_min_chars = markdown_item_parser_min_chars
        self._company_mapping: list[dict[str, Any]] | None = None
        self._configure_edgartools_identity()

    def _configure_edgartools_identity(self) -> None:
        edgar = self._edgar()
        if not edgar or not hasattr(edgar, "set_identity"):
            return

        # EdgarTools requires an SEC-friendly identity before it performs live requests.
        edgar.set_identity(self.settings.edgar_identity)

    def resolve_by_ticker(self, ticker: str) -> ResolvedTarget | None:
        edgar_target = self._resolve_with_edgartools(ticker, matched_input=ticker, confidence=1.0)
        if edgar_target:
            return edgar_target

        normalized = ticker.strip().upper()
        for row in self._load_company_mapping():
            if row["ticker"].upper() == normalized:
                return self._target_from_mapping(row, matched_input=ticker, confidence=1.0)
        return None

    def resolve_by_cik(self, cik: str) -> ResolvedTarget | None:
        edgar_target = self._resolve_with_edgartools(cik, matched_input=cik, confidence=1.0)
        if edgar_target:
            return edgar_target

        normalized = str(int(cik.strip())) if cik.strip().isdigit() else cik.strip()
        for row in self._load_company_mapping():
            if str(row["cik"]) == normalized:
                return self._target_from_mapping(row, matched_input=cik, confidence=1.0)
        return None

    def resolve_by_company_name(self, company_name: str) -> list[ResolvedTarget]:
        edgar_matches = self._resolve_company_name_with_edgartools(company_name)
        if edgar_matches:
            return edgar_matches

        normalized = _normalize_name(company_name)
        matches = [
            self._target_from_mapping(row, matched_input=company_name, confidence=0.95)
            for row in self._load_company_mapping()
            if _normalize_name(row["name"]) == normalized
        ]
        return matches

    def fetch_recent_filings(
        self,
        target: ResolvedTarget,
        forms: tuple[str, ...] = ("10-K", "10-Q", "8-K", "S-1", "S-1/A"),
    ) -> list[FilingMetadata]:
        edgar_filings = self._fetch_recent_filings_with_edgartools(target, forms)
        if edgar_filings:
            return edgar_filings

        submissions = self._fetch_submissions(target)
        recent = submissions.get("filings", {}).get("recent", {})
        form_values = recent.get("form", [])
        accession_values = recent.get("accessionNumber", [])
        filing_date_values = recent.get("filingDate", [])
        report_date_values = recent.get("reportDate", [])

        latest_by_form: dict[str, FilingMetadata] = {}
        for index, form in enumerate(form_values):
            if form not in forms or form in latest_by_form:
                continue

            accession = accession_values[index]
            cik_no_padding = str(int(target.cik))
            accession_no_dashes = accession.replace("-", "")
            latest_by_form[form] = FilingMetadata(
                form=form,
                filing_date=_safe_index(filing_date_values, index),
                accession_number=accession,
                period_of_report=_safe_index(report_date_values, index),
                url=f"https://www.sec.gov/Archives/edgar/data/{cik_no_padding}/{accession_no_dashes}/{accession}-index.html",
            )

        return list(latest_by_form.values())

    def _edgar(self) -> Any | None:
        if not self._use_edgartools:
            return None
        if self._edgar_module is not None:
            return self._edgar_module
        try:
            import edgar
        except ImportError:
            return None

        self._edgar_module = edgar
        return edgar

    def _resolve_with_edgartools(self, identifier: str, matched_input: str, confidence: float) -> ResolvedTarget | None:
        edgar = self._edgar()
        if not edgar or not identifier.strip():
            return None

        context = self._start_provider_request(
            operation="edgartools_company_lookup",
            request_params={"identifier": identifier.strip()},
        )
        try:
            company = edgar.Company(identifier.strip())
        except Exception as error:
            self._record_provider_error(context, error)
            return None

        self._record_provider_success(context, [_edgartools_company_raw_record(company, context, datetime.now(UTC))])
        return self._target_from_edgartools_company(company, matched_input=matched_input, confidence=confidence)

    def _resolve_company_name_with_edgartools(self, company_name: str) -> list[ResolvedTarget]:
        edgar = self._edgar()
        if not edgar or not hasattr(edgar, "find_company"):
            return []

        context = self._start_provider_request(
            operation="edgartools_company_search",
            request_params={"company_name": company_name, "top_n": 10},
        )
        try:
            search_results = edgar.find_company(company_name, top_n=10)
        except Exception as error:
            self._record_provider_error(context, error)
            return []

        rows = _rows_from_search_results(search_results)
        self._record_provider_success(
            context,
            [_edgartools_search_raw_record(row, context, datetime.now(UTC), index) for index, row in enumerate(rows, start=1)],
        )
        normalized_query = _normalize_name(company_name)
        exact_rows = [row for row in rows if _normalize_name(str(row.get("company", ""))) == normalized_query]
        return [
            self._target_from_search_result(row, matched_input=company_name)
            for row in exact_rows
            if row.get("ticker") and row.get("cik")
        ]

    def _fetch_recent_filings_with_edgartools(
        self,
        target: ResolvedTarget,
        forms: tuple[str, ...],
    ) -> list[FilingMetadata]:
        edgar = self._edgar()
        if not edgar:
            return []

        company_context = self._start_provider_request(
            operation="edgartools_company_for_filings",
            target=target,
            request_params={"identifier": target.cik},
        )
        try:
            company = edgar.Company(target.cik)
        except Exception as error:
            self._record_provider_error(company_context, error)
            return []
        self._record_provider_success(company_context, [_edgartools_company_raw_record(company, company_context, datetime.now(UTC))])

        filings: list[FilingMetadata] = []
        for form in forms:
            context = self._start_provider_request(
                operation="edgartools_recent_filing",
                target=target,
                request_params={"form": form, "trigger_full_load": False},
            )
            try:
                form_filings = company.get_filings(form=form, trigger_full_load=False)
                latest_filing = form_filings.latest() if form_filings else None
            except Exception as error:
                self._record_provider_error(context, error)
                continue

            if latest_filing:
                filing = self._filing_from_edgartools(target, latest_filing)
                self._record_provider_success(
                    context,
                    [_filing_raw_record(filing, context, datetime.now(UTC), provider_method="edgartools:get_filings.latest")],
                )
                filings.append(filing)
            else:
                self._record_provider_success(context, [])

        return filings

    def _target_from_edgartools_company(
        self,
        company: Any,
        matched_input: str,
        confidence: float,
    ) -> ResolvedTarget | None:
        tickers = list(getattr(company, "tickers", []) or [])
        exchanges = list(getattr(company, "exchanges", []) or [])
        preferred_ticker = matched_input.strip().upper()
        ticker = preferred_ticker if preferred_ticker in {ticker.upper() for ticker in tickers} else _first_or_none(tickers)
        if not ticker:
            return None

        return ResolvedTarget(
            canonical_name=str(getattr(company, "name")),
            ticker=str(ticker),
            cik=str(getattr(company, "cik")).zfill(10),
            exchange=_first_or_none(exchanges),
            sic=str(getattr(company, "sic")) if getattr(company, "sic", None) else None,
            resolution_confidence=confidence,
            matched_input=matched_input,
            source_provenance=["edgartools:Company"],
        )

    def _target_from_search_result(self, row: dict[str, Any], matched_input: str) -> ResolvedTarget:
        return ResolvedTarget(
            canonical_name=str(row.get("company")),
            ticker=str(row.get("ticker")),
            cik=str(row.get("cik")).zfill(10),
            resolution_confidence=min(float(row.get("score", 95)) / 100, 0.99),
            matched_input=matched_input,
            source_provenance=["edgartools:find_company"],
        )

    def _filing_from_edgartools(self, target: ResolvedTarget, filing: Any) -> FilingMetadata:
        accession = str(getattr(filing, "accession_no", getattr(filing, "accession_number", "")))
        cik_no_padding = str(int(target.cik))
        accession_no_dashes = accession.replace("-", "")
        return FilingMetadata(
            form=str(getattr(filing, "form")),
            filing_date=str(getattr(filing, "filing_date")) if getattr(filing, "filing_date", None) else None,
            accession_number=accession,
            period_of_report=str(getattr(filing, "report_date")) if getattr(filing, "report_date", None) else None,
            url=f"https://www.sec.gov/Archives/edgar/data/{cik_no_padding}/{accession_no_dashes}/{accession}-index.html",
        )

    def source_document_for_filing(
        self,
        target: ResolvedTarget,
        filing: FilingMetadata,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        return SourceDocument(
            source_id="edgar",
            source_dimension=source_dimension,
            source_type=SourceType.sec_filing,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=filing.url,
            filing_accession=filing.accession_number,
            metadata=filing.model_dump(mode="json"),
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )

    def fetch_filing_text_document(
        self,
        target: ResolvedTarget,
        filing: FilingMetadata,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.B,
        source_dimension: str | None = None,
        text_scope: str = "business_description",
    ) -> SourceDocument:
        """Fetch structured filing sections and cache the one relevant to a profile dimension."""

        extraction = self._fetch_filing_text_with_edgartools(target, filing, text_scope, source_dimension)
        if not extraction:
            raise ValueError(f"Could not retrieve structured filing section for accession {filing.accession_number}")

        metadata = {
            **filing.model_dump(mode="json"),
            "text_retrieval_method": extraction.retrieval_method,
            "text_scope": extraction.text_scope,
            "raw_text_char_count": extraction.raw_text_char_count,
            "cached_text_char_count": extraction.cached_text_char_count,
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
            raw_text=extraction.selected_text,
            metadata=metadata,
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )

    def source_document_for_mapping(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> SourceDocument:
        return SourceDocument(
            source_id="edgar",
            source_dimension=source_dimension,
            source_type=SourceType.sec_company_mapping,
            source_strength=source_strength,
            target_cik=target.cik,
            target_ticker=target.ticker,
            url=self.company_tickers_exchange_url,
            metadata=target.model_dump(mode="json"),
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )

    def _fetch_filing_text_with_edgartools(
        self,
        target: ResolvedTarget,
        filing: FilingMetadata,
        text_scope: str,
        source_dimension: str | None,
    ) -> FilingTextExtraction | None:
        edgar = self._edgar()
        if not edgar or not hasattr(edgar, "Filing"):
            return None

        filing_object = self._edgartools_filing_object(edgar, target, filing)
        if not filing_object:
            return None

        return self._fetch_structured_filing_section(
            target,
            filing,
            filing_object,
            text_scope,
            source_dimension,
        )

    def _edgartools_filing_object(self, edgar: Any, target: ResolvedTarget, filing: FilingMetadata) -> Any | None:
        get_by_accession_number = getattr(edgar, "get_by_accession_number", None)
        if callable(get_by_accession_number):
            try:
                return get_by_accession_number(filing.accession_number, show_progress=False)
            except Exception:
                return None

        filing_class = getattr(edgar, "Filing", None)
        if not callable(filing_class):
            return None
        try:
            return filing_class(
                cik=int(target.cik),
                company=target.canonical_name,
                form=filing.form,
                filing_date=filing.filing_date or filing.period_of_report or "",
                accession_no=filing.accession_number,
            )
        except Exception:
            return None

    def _fetch_structured_filing_section(
        self,
        target: ResolvedTarget,
        filing: FilingMetadata,
        filing_object: Any,
        text_scope: str,
        source_dimension: str | None,
    ) -> FilingTextExtraction | None:
        obj_method = getattr(filing_object, "obj", None) or getattr(filing_object, "data_object", None)
        if not callable(obj_method):
            return None

        context = self._start_provider_request(
            operation="edgartools_filing_structured_object",
            target=target,
            source_dimension=source_dimension,
            request_params={"accession_number": filing.accession_number, "form": filing.form, "text_scope": text_scope},
        )
        try:
            report_object = obj_method()
        except Exception as error:
            self._record_provider_error(context, error)
            return None

        if not report_object:
            self._record_request(
                context,
                DataSourceRequestStatus.error,
                None,
                [],
                error_message=f"edgartools:Filing.obj returned no report object for accession {filing.accession_number}",
            )
            return None

        sections = _structured_sections_from_report_object(report_object, filing)
        selected_text, selected_scope = _select_structured_section(sections, filing, text_scope)
        payload = _structured_report_payload(report_object, filing, sections, selected_scope)
        audit_text, audit_text_representation = _structured_raw_text_for_audit(
            filing_object,
            selected_text,
            report_object,
            payload,
        )
        retrieval_method = "edgartools:Filing.obj"

        if not selected_text and self.markdown_item_parser_enabled:
            parsed_section = _parse_markdown_item_section(
                audit_text or "",
                filing,
                text_scope,
                self.markdown_item_parser_min_chars,
            )
            if parsed_section:
                selected_text = parsed_section.text
                selected_scope = parsed_section.selected_scope
                sections = {**sections, parsed_section.section_name: parsed_section.text}
                payload = _structured_report_payload(report_object, filing, sections, selected_scope)
                payload["markdown_item_parser"] = {
                    "enabled": True,
                    "result": "matched",
                    "start_item": parsed_section.start_item,
                    "end_item": parsed_section.end_item,
                    "min_chars": self.markdown_item_parser_min_chars,
                    "selected_char_count": len(parsed_section.text),
                }
                retrieval_method = "edgartools:Filing.markdown:item_parser"
            else:
                payload["markdown_item_parser"] = {
                    "enabled": True,
                    "result": "no_confident_item_boundary",
                    "min_chars": self.markdown_item_parser_min_chars,
                }

        payload["text_retrieval_method"] = retrieval_method
        raw_record = _filing_structured_raw_record(
            filing,
            context,
            datetime.now(UTC),
            report_object,
            payload,
            audit_text,
            selected_scope,
            audit_text_representation,
            retrieval_method,
        )
        if not selected_text:
            self._record_request(
                context,
                DataSourceRequestStatus.error,
                None,
                [raw_record],
                error_message=(
                    "edgartools:Filing.obj did not return the required structured section "
                    f"for form={filing.form} text_scope={text_scope}"
                ),
            )
            return None

        self._record_provider_success(context, [raw_record])

        return FilingTextExtraction(
            selected_text=selected_text,
            retrieval_method=retrieval_method,
            text_scope=selected_scope,
            raw_text_char_count=len(audit_text or "") if retrieval_method != "edgartools:Filing.obj" else sum(
                len(section_text) for section_text in sections.values()
            ),
            cached_text_char_count=len(selected_text),
            structured_sections=sections,
        )

    def _load_company_mapping(self) -> list[dict[str, Any]]:
        if self._company_mapping is not None:
            return self._company_mapping

        payload, _raw_records = self._get_json(
            operation="company_tickers_exchange",
            url=self.company_tickers_exchange_url,
            headers={"User-Agent": self.settings.sec_user_agent},
            raw_records_from_payload=_company_mapping_raw_records_from_payload,
        )
        fields = payload["fields"]
        self._company_mapping = [dict(zip(fields, row, strict=False)) for row in payload["data"]]
        return self._company_mapping

    def _fetch_submissions(self, target: ResolvedTarget) -> dict[str, Any]:
        cik_padded = target.cik.zfill(10)
        payload, _raw_records = self._get_json(
            operation="submissions",
            url=self.submissions_url_template.format(cik=cik_padded),
            headers={"User-Agent": self.settings.sec_user_agent},
            target=target,
            raw_records_from_payload=_submission_filing_raw_records_from_payload,
        )
        return payload

    def _target_from_mapping(self, row: dict[str, Any], matched_input: str, confidence: float) -> ResolvedTarget:
        return ResolvedTarget(
            canonical_name=row["name"],
            ticker=row["ticker"],
            cik=str(row["cik"]).zfill(10),
            exchange=row.get("exchange"),
            resolution_confidence=confidence,
            matched_input=matched_input,
            source_provenance=["edgar:company_tickers_exchange"],
        )


def _edgartools_company_raw_record(
    company: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> DataSourceRawRecord:
    raw_payload = {
        "cik": getattr(company, "cik", None),
        "name": getattr(company, "name", None),
        "tickers": list(getattr(company, "tickers", []) or []),
        "exchanges": list(getattr(company, "exchanges", []) or []),
        "sic": getattr(company, "sic", None),
    }
    ticker = _first_or_none(raw_payload["tickers"])
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=SourceType.sec_company_mapping,
        target_cik=str(raw_payload["cik"]).zfill(10) if raw_payload.get("cik") else context.target_cik,
        target_ticker=str(ticker) if ticker else context.target_ticker,
        record_id=str(raw_payload["cik"]) if raw_payload.get("cik") else context.url,
        raw_payload=raw_payload,
        metadata={"provider_method": context.operation, "record_role": "company_mapping"},
        retrieved_at=retrieved_at,
    )


def _edgartools_search_raw_record(
    row: dict[str, Any],
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    index: int,
) -> DataSourceRawRecord:
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=SourceType.sec_company_mapping,
        target_cik=str(row.get("cik")).zfill(10) if row.get("cik") else context.target_cik,
        target_ticker=str(row.get("ticker")) if row.get("ticker") else context.target_ticker,
        record_id=str(row.get("cik") or row.get("ticker") or index),
        raw_payload=row,
        metadata={"provider_method": context.operation, "record_role": "company_mapping", "record_index": index},
        retrieved_at=retrieved_at,
    )


def _company_mapping_raw_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    if not isinstance(payload, dict):
        return []

    fields = payload.get("fields", [])
    rows = payload.get("data", [])
    if not isinstance(fields, list) or not isinstance(rows, list):
        return []

    raw_records: list[DataSourceRawRecord] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            continue
        raw_payload = dict(zip(fields, row, strict=False))
        raw_records.append(
            DataSourceRawRecord(
                request_id=context.request_id,
                source_id=context.source_id,
                provider=context.provider,
                source_dimension=context.source_dimension,
                source_type=SourceType.sec_company_mapping,
                target_cik=str(raw_payload.get("cik")).zfill(10) if raw_payload.get("cik") else None,
                target_ticker=str(raw_payload.get("ticker")) if raw_payload.get("ticker") else None,
                record_id=str(raw_payload.get("cik") or raw_payload.get("ticker") or index),
                url=context.url,
                raw_payload=raw_payload,
                metadata={"record_role": "company_mapping", "record_index": index},
                retrieved_at=retrieved_at,
            )
        )
    return raw_records


def _submission_filing_raw_records_from_payload(
    payload: Any,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
) -> list[DataSourceRawRecord]:
    if not isinstance(payload, dict):
        return []

    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []

    array_fields = {key: value for key, value in recent.items() if isinstance(value, list)}
    row_count = max((len(value) for value in array_fields.values()), default=0)
    raw_records: list[DataSourceRawRecord] = []
    for index in range(row_count):
        # SEC submissions expose recent filings as parallel arrays; position is the raw record boundary.
        raw_payload = {key: _safe_index(value, index) for key, value in array_fields.items()}
        accession = raw_payload.get("accessionNumber")
        raw_records.append(
            DataSourceRawRecord(
                request_id=context.request_id,
                source_id=context.source_id,
                provider=context.provider,
                source_dimension=context.source_dimension,
                source_type=SourceType.sec_filing,
                target_cik=context.target_cik,
                target_ticker=context.target_ticker,
                record_id=str(accession or f"{context.target_cik}:{index + 1}"),
                url=_filing_index_url(context.target_cik, accession),
                filing_accession=str(accession) if accession else None,
                title=str(raw_payload.get("form")) if raw_payload.get("form") else None,
                published_at=str(raw_payload.get("filingDate")) if raw_payload.get("filingDate") else None,
                raw_payload=raw_payload,
                metadata={"record_role": "filing_metadata", "record_index": index + 1},
                retrieved_at=retrieved_at,
            )
        )
    return raw_records


def _filing_raw_record(
    filing: FilingMetadata,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    provider_method: str,
) -> DataSourceRawRecord:
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=SourceType.sec_filing,
        target_cik=context.target_cik,
        target_ticker=context.target_ticker,
        record_id=filing.accession_number,
        url=filing.url,
        filing_accession=filing.accession_number,
        title=filing.form,
        published_at=filing.filing_date,
        raw_payload=filing.model_dump(mode="json"),
        metadata={"provider_method": provider_method, "record_role": "filing_metadata"},
        retrieved_at=retrieved_at,
    )


def _filing_structured_raw_record(
    filing: FilingMetadata,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    report_object: Any,
    payload: dict[str, Any],
    raw_text: str | None,
    selected_scope: str,
    raw_text_representation: str | None,
    text_retrieval_method: str,
) -> DataSourceRawRecord:
    return DataSourceRawRecord(
        request_id=context.request_id,
        source_id=context.source_id,
        provider=context.provider,
        source_dimension=context.source_dimension,
        source_type=SourceType.sec_filing,
        target_cik=context.target_cik,
        target_ticker=context.target_ticker,
        record_id=f"{filing.accession_number}:{text_retrieval_method}",
        url=filing.url,
        filing_accession=filing.accession_number,
        title=filing.form,
        published_at=filing.filing_date,
        raw_payload=payload,
        raw_text=raw_text,
        metadata={
            "provider_method": "edgartools:Filing.obj",
            "text_retrieval_method": text_retrieval_method,
            "record_role": "filing_structured_object",
            "obj_type": type(report_object).__name__,
            "selected_scope": selected_scope,
            "raw_text_char_count": len(raw_text or ""),
            "raw_text_representation": raw_text_representation,
        },
        retrieved_at=retrieved_at,
    )


def _filing_index_url(cik: str | None, accession: Any) -> str | None:
    if not cik or not accession:
        return None
    accession_text = str(accession)
    cik_no_padding = str(int(cik)) if str(cik).isdigit() else str(cik)
    accession_no_dashes = accession_text.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_no_padding}/{accession_no_dashes}/{accession_text}-index.html"


def _structured_raw_text_for_audit(
    filing_object: Any,
    selected_text: str | None,
    report_object: Any,
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    markdown_method = getattr(filing_object, "markdown", None)
    if callable(markdown_method):
        try:
            markdown = markdown_method()
        except Exception:
            markdown = None
        if isinstance(markdown, str) and markdown.strip():
            return markdown, "edgartools:Filing.markdown:audit"

    if selected_text:
        return selected_text, "structured_selected_section"

    section_markdown = _sections_to_markdown(payload.get("sections", {}))
    if section_markdown:
        return section_markdown, "structured_sections_markdown"

    object_text = _clean_filing_text(_coerce_report_text(report_object))
    if object_text:
        return object_text, "structured_object_text"

    return None, None


def _parse_markdown_item_section(
    markdown: str,
    filing: FilingMetadata,
    text_scope: str,
    min_chars: int,
) -> MarkdownItemSection | None:
    spec = _markdown_item_parser_spec(filing, text_scope)
    if not spec or not markdown.strip():
        return None

    section_name, selected_scope, start_items, end_items = spec
    normalized_markdown = markdown.replace("\r", "\n")
    headings = _markdown_item_headings(normalized_markdown)
    candidates: list[tuple[int, MarkdownItemSection]] = []
    for index, heading in enumerate(headings):
        if heading.item_code not in start_items:
            continue

        end_heading = next(
            (candidate for candidate in headings[index + 1 :] if candidate.item_code in end_items),
            None,
        )
        if not end_heading:
            continue

        # Include the Item heading itself so downstream audit text keeps SEC section provenance.
        text = _clean_filing_text(normalized_markdown[heading.start : end_heading.start])
        if len(text) < min_chars:
            continue

        candidates.append(
            (
                heading.start,
                MarkdownItemSection(
                    text=text,
                    section_name=section_name,
                    selected_scope=selected_scope,
                    start_item=heading.item_code,
                    end_item=end_heading.item_code,
                ),
            )
        )

    if not candidates:
        return None

    # Later Item headings are preferred because table-of-contents entries usually appear first.
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _markdown_item_parser_spec(
    filing: FilingMetadata,
    text_scope: str,
) -> tuple[str, str, set[str], set[str]] | None:
    normalized_form = filing.form.strip().upper()
    if text_scope == "company_strategy":
        if normalized_form.startswith("10-K"):
            return "management_discussion", "item_7_mdna", {"7"}, {"7A", "8"}
        if normalized_form.startswith("10-Q"):
            return "management_discussion", "item_2_mdna", {"2"}, {"3", "4"}
        return None

    if normalized_form.startswith("10-K"):
        return "business", "item_1_business", {"1"}, {"1A", "2"}
    return None


def _markdown_item_headings(markdown: str) -> list[MarkdownItemHeading]:
    headings: list[MarkdownItemHeading] = []
    for match in _ITEM_HEADING_RE.finditer(markdown):
        item_code = re.sub(r"\s+", "", match.group("item").upper())
        headings.append(MarkdownItemHeading(item_code=item_code, start=match.start(), end=match.end()))
    return headings


def _sections_to_markdown(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    parts: list[str] = []
    for section_name, section_text in value.items():
        text = _clean_filing_text(str(section_text)) if section_text else ""
        if not text:
            continue
        parts.append(f"## {section_name}\n\n{text}")
    return "\n\n".join(parts) or None


def _structured_sections_from_report_object(report_object: Any, filing: FilingMetadata) -> dict[str, str]:
    sections: dict[str, str] = {}

    def add_section(name: str, value: Any) -> None:
        text = _clean_filing_text(_coerce_report_text(value))
        if text:
            sections[name] = text

    normalized_form = filing.form.strip().upper()
    add_section("business", _report_attribute(report_object, "business"))
    add_section("risk_factors", _report_attribute(report_object, "risk_factors"))
    add_section("management_discussion", _report_attribute(report_object, "management_discussion"))

    if "business" not in sections:
        add_section("business", _report_item(report_object, "Item 1"))
    if "risk_factors" not in sections:
        add_section("risk_factors", _report_item(report_object, "Item 1A"))
    if "management_discussion" not in sections:
        if normalized_form.startswith("10-K"):
            add_section("management_discussion", _report_item(report_object, "Item 7"))
        elif normalized_form.startswith("10-Q"):
            # Ten-Q has no convenience property in edgartools 3.15.1, but Item 2 is MD&A.
            add_section("management_discussion", _report_item(report_object, "Item 2"))

    if normalized_form.startswith("8-K"):
        add_section("current_report", _report_attribute(report_object, "text"))

    return sections


def _select_structured_section(
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


def _structured_report_payload(
    report_object: Any,
    filing: FilingMetadata,
    sections: dict[str, str],
    selected_scope: str,
) -> dict[str, Any]:
    items = _json_safe_list(_report_attribute(report_object, "items"))
    available_properties = [
        property_name
        for property_name in (
            "business",
            "risk_factors",
            "management_discussion",
            "financials",
            "income_statement",
            "balance_sheet",
            "cash_flow_statement",
            "reports",
            "press_releases",
        )
        if property_name in dir(report_object)
    ]
    return {
        **filing.model_dump(mode="json"),
        "provider_method": "edgartools:Filing.obj",
        "record_role": "filing_structured_object",
        "obj_type": type(report_object).__name__,
        "items": items,
        "available_properties": available_properties,
        "selected_scope": selected_scope,
        "section_char_counts": {section_name: len(section_text) for section_name, section_text in sections.items()},
        "sections": sections,
    }


def _report_attribute(report_object: Any, name: str) -> Any:
    try:
        return getattr(report_object, name, None)
    except Exception:
        return None


def _report_item(report_object: Any, item_name: str) -> Any:
    try:
        return report_object[item_name]
    except Exception:
        return None


def _coerce_report_text(value: Any) -> str:
    if value is None:
        return ""
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    if isinstance(value, str):
        return value
    return str(value)


def _json_safe_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return []


def _normalize_name(value: str) -> str:
    return " ".join(value.lower().replace(",", " ").replace(".", " ").split())


def _safe_index(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def _first_or_none(values: list[Any]) -> Any | None:
    return values[0] if values else None


def _clean_filing_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line)).strip()


def _full_text_scope_for_form(form: str) -> str:
    normalized_form = form.strip().upper()
    if normalized_form.startswith("8-K"):
        return "full_8k_current_report"
    if normalized_form.startswith("S-1"):
        return "full_s1_registration_statement"
    return "full_filing_text"


def _rows_from_search_results(search_results: Any) -> list[dict[str, Any]]:
    results = getattr(search_results, "results", search_results)
    if hasattr(results, "to_dict"):
        return list(results.to_dict(orient="records"))
    if isinstance(results, list):
        return [dict(row) for row in results]
    return []
