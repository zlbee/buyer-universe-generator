"""Official company page adapter for best-effort Phase 2 profile enrichment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from src.config import Settings
from src.domain import ResolvedTarget, SourceDocument, SourceStrength, SourceType


class CompanyPageClient:
    """Fetches official pages only when a structured source already discovered the URL."""

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True)

    def fetch_official_page(
        self,
        target: ResolvedTarget,
        url: str,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> SourceDocument | None:
        documents = self.fetch_official_pages(target, url, ttl_hours, source_strength, source_dimension)
        return documents[0] if documents else None

    def fetch_official_pages(
        self,
        target: ResolvedTarget,
        url: str,
        ttl_hours: int,
        source_strength: SourceStrength = SourceStrength.A,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        if not _is_http_url(url):
            return []

        response = self.http_client.get(url, headers={"User-Agent": self.settings.sec_user_agent})
        response.raise_for_status()

        documents: list[SourceDocument] = []
        for candidate_url, page_role in _prioritized_company_page_links(response.text, url):
            document = self._fetch_page_document(
                target,
                candidate_url,
                ttl_hours,
                source_strength,
                source_dimension,
                page_role=page_role,
                discovered_from=url,
            )
            if document:
                documents.append(document)

        if documents:
            return documents

        return []

    def _fetch_page_document(
        self,
        target: ResolvedTarget,
        url: str,
        ttl_hours: int,
        source_strength: SourceStrength,
        source_dimension: str | None,
        page_role: str,
        discovered_from: str,
    ) -> SourceDocument | None:
        try:
            response = self.http_client.get(url, headers={"User-Agent": self.settings.sec_user_agent})
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        return _source_document_from_html(
            target,
            url,
            response.text,
            ttl_hours,
            source_strength,
            source_dimension,
            page_role=page_role,
            discovered_from=discovered_from,
        )


def _source_document_from_html(
    target: ResolvedTarget,
    url: str,
    html: str,
    ttl_hours: int,
    source_strength: SourceStrength,
    source_dimension: str | None,
    page_role: str,
    discovered_from: str,
) -> SourceDocument | None:
    text = _clean_page_text(html)
    if not text:
        return None

    return SourceDocument(
        source_id="company_pages",
        source_dimension=source_dimension,
        source_type=SourceType.company_page,
        source_strength=source_strength,
        target_cik=target.cik,
        target_ticker=target.ticker,
        url=url,
        raw_text=text,
        metadata={
            "discovered_from": discovered_from,
            "domain": urlparse(url).netloc,
            "page_role": page_role,
        },
        retrieved_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
    )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _prioritized_company_page_links(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _AnchorParser()
    parser.feed(html)

    candidates: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for href, text in parser.links:
        absolute_url = urljoin(base_url, href).split("#", 1)[0]
        if absolute_url in seen or not _is_http_url(absolute_url):
            continue
        seen.add(absolute_url)

        role_priority = _classify_company_page_link(absolute_url, text)
        if role_priority:
            priority, page_role = role_priority
            candidates.append((priority, absolute_url, page_role))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(url, page_role) for _priority, url, page_role in candidates[:2]]


def _classify_company_page_link(url: str, text: str) -> tuple[int, str] | None:
    haystack = f"{text} {url}".casefold()
    # Investor and corporate overview pages are more useful than storefront homepages for company profile evidence.
    if any(token in haystack for token in ("investor", "investors", "investor-relations", "/ir", "ir.")):
        return (0, "investor_relations")
    if any(token in haystack for token in ("about us", "about", "our story", "who we are", "company")):
        return (1, "corporate_about")
    return None


def _clean_page_text(value: str) -> str:
    # This is intentionally light; Phase 2 only needs a supporting official-page excerpt, not a crawler.
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

    in_tag = False
    output: list[str] = []
    for char in without_scripts:
        if char == "<":
            in_tag = True
            output.append(" ")
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            output.append(char)
    return " ".join("".join(output).split())[:20_000]


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = next((value for name, value in attrs if name.casefold() == "href"), None)
        if href:
            self._current_href = href
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or not self._current_href:
            return
        self.links.append((self._current_href, " ".join("".join(self._current_text_parts).split())))
        self._current_href = None
        self._current_text_parts = []
