"""Official company page adapter for best-effort Phase 2 profile enrichment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

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
        if not _is_http_url(url):
            return None

        response = self.http_client.get(url, headers={"User-Agent": self.settings.sec_user_agent})
        response.raise_for_status()
        text = _clean_page_text(response.text)
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
            metadata={"discovered_from": "structured_profile_metadata", "domain": urlparse(url).netloc},
            retrieved_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours) if ttl_hours else None,
        )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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
