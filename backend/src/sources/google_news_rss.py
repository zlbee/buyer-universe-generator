"""Google News RSS adapter for supplemental news discovery."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

import httpx

from src.config import Settings
from src.domain import DataSourceRawRecord, ResolvedTarget, SourceDocument, SourceStrength, SourceType
from src.sources.base import DataSourceRequestContext, DataSourceRequestRecorder, ExternalDataSourceClient, source_document_from_raw_record


logger = logging.getLogger(__name__)


class GoogleNewsRssClient(ExternalDataSourceClient):
    """Fetches Google News RSS search results as provider-neutral news documents."""

    rss_url = "https://news.google.com/rss"
    search_url = "https://news.google.com/rss/search"

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        request_recorder: DataSourceRequestRecorder | None = None,
        provider: str = "google_news_rss",
        default_language: str | None = "en-US",
        default_country: str | None = "US",
        default_edition: str | None = "US:en",
    ) -> None:
        super().__init__(
            settings,
            source_id="google_news_rss",
            provider=provider,
            http_client=http_client,
            request_recorder=request_recorder,
        )
        self.default_language = default_language
        self.default_country = default_country
        self.default_edition = default_edition

    def fetch_target_articles(
        self,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 5,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
    ) -> list[SourceDocument]:
        """Search Google News RSS for target identity terms."""

        return self.fetch_articles_for_query(
            f'"{target.canonical_name}" OR {target.ticker}',
            target,
            ttl_hours,
            page_size=page_size,
            source_strength=source_strength,
            source_dimension=source_dimension,
        )

    def fetch_articles_for_query(
        self,
        query: str,
        target: ResolvedTarget,
        ttl_hours: int,
        page_size: int = 10,
        source_strength: SourceStrength = SourceStrength.C,
        source_dimension: str | None = None,
        language: str | None = None,
        country: str | None = None,
        edition: str | None = None,
    ) -> list[SourceDocument]:
        """Run a Google News RSS search query and return normalized article documents."""

        params = {
            "q": query,
            "hl": language or self.default_language,
            "gl": country or self.default_country,
            "ceid": edition or self.default_edition,
        }
        params = {key: value for key, value in params.items() if value}

        logger.info("Google News RSS request: url=%s params=%s", self.search_url, params)
        _text, raw_records = self._get_text(
            operation="rss_search_query",
            url=self.search_url,
            params=params,
            target=target,
            source_dimension=source_dimension,
            raw_records_from_text=_google_news_rss_raw_records_from_text,
        )
        logger.info("Google News RSS response parsed: article_count=%s", len(raw_records))

        return [
            source_document_from_raw_record(
                raw_record,
                source_strength=source_strength,
                ttl_hours=ttl_hours,
            )
            for raw_record in raw_records[:page_size]
            if raw_record.url
        ]


def _google_news_rss_raw_records_from_text(
    text: str,
    context: DataSourceRequestContext,
    retrieved_at: datetime,
    _response: httpx.Response,
) -> list[DataSourceRawRecord]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    raw_records: list[DataSourceRawRecord] = []
    for index, item in enumerate(_children_by_local_name(root, "item"), start=1):
        title = _xml_child_text(item, "title")
        link = _xml_child_text(item, "link")
        published_at = _rss_datetime(_xml_child_text(item, "pubDate"))
        description = _clean_html(_xml_child_text(item, "description"))
        source = _source_metadata(item)
        guid = _xml_child_text(item, "guid")
        raw_payload = {
            "title": title,
            "url": link,
            "publishedAt": published_at,
            "description": description,
            "source": source,
            "guid": guid,
            "query": context.request_params.get("q"),
            "provider_method": "google_news_rss_search",
        }
        raw_records.append(
            DataSourceRawRecord(
                request_id=context.request_id,
                source_id=context.source_id,
                provider=context.provider,
                source_dimension=context.source_dimension,
                source_type=SourceType.news_article,
                target_cik=context.target_cik,
                target_ticker=context.target_ticker,
                record_id=str(link or guid or title or index),
                url=link,
                title=title,
                published_at=published_at,
                raw_payload={key: value for key, value in raw_payload.items() if value is not None},
                raw_text=" ".join(value for value in (title, description) if value) or None,
                metadata={"record_role": "rss_item", "record_index": index},
                retrieved_at=retrieved_at,
            )
        )
    return raw_records


def _children_by_local_name(element: ET.Element, name: str) -> list[ET.Element]:
    matches: list[ET.Element] = []
    for child in element.iter():
        if _local_name(child.tag) == name:
            matches.append(child)
    return matches


def _xml_child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) != name or child.text is None:
            continue
        text = child.text.strip()
        return text or None
    return None


def _source_metadata(item: ET.Element) -> dict[str, str] | None:
    for child in item:
        if _local_name(child.tag) != "source":
            continue
        name = child.text.strip() if child.text else ""
        url = child.attrib.get("url")
        metadata = {"name": name, "url": url}
        return {key: value for key, value in metadata.items() if value}
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rss_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo:
        return parsed.astimezone(UTC).isoformat()
    return parsed.replace(tzinfo=UTC).isoformat()


def _clean_html(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", value)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return text or None
