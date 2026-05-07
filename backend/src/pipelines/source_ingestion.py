"""Source ingestion orchestration for Phase 1."""

from __future__ import annotations

from src.domain import FilingMetadata, SourceDocument, SourceStrength, SourceType, TargetIngestionResult
from src.pipelines.target_resolution import TargetResolver
from src.repositories.source_cache import SourceCache
from src.sources.newsapi import NewsApiClient
from src.sources.polygon import PolygonClient
from src.sources.sec import SecEdgarClient
from src.retrieval.policy import DataSourceStrategy


class SourceIngestionService:
    """Resolves a target and caches the source metadata needed by Phase 2."""

    def __init__(
        self,
        strategy: DataSourceStrategy,
        resolver: TargetResolver,
        cache: SourceCache,
        edgar_client: SecEdgarClient,
        polygon_client: PolygonClient | None = None,
        newsapi_client: NewsApiClient | None = None,
    ) -> None:
        self.strategy = strategy
        self.resolver = resolver
        self.cache = cache
        self.edgar_client = edgar_client
        self.polygon_client = polygon_client
        self.newsapi_client = newsapi_client

    def ingest(self, query: str) -> TargetIngestionResult:
        target = self.resolver.resolve(query)
        warnings: list[str] = []
        documents = self.cache.get_valid_documents(target.cik)

        if not any(document.source_type == SourceType.sec_company_mapping for document in documents):
            mapping_source = self.strategy.selected_source("target_identity", "edgar", include_disabled=True)
            mapping_document = self.edgar_client.source_document_for_mapping(
                target,
                self.strategy.cache_ttl_hours("edgar"),
                source_strength=mapping_source.source_strength if mapping_source else SourceStrength.A,
                source_dimension=mapping_source.dimension_id if mapping_source else None,
            )
            self.cache.save_document(mapping_document)
            documents.append(mapping_document)

        filings = self._filings_from_documents(documents)
        if not filings:
            filing_source = self.strategy.selected_source("sec_filing_metadata", "edgar", include_disabled=True)
            filings = self.edgar_client.fetch_recent_filings(target)
            filing_documents = [
                self.edgar_client.source_document_for_filing(
                    target,
                    filing,
                    self.strategy.cache_ttl_hours("edgar"),
                    source_strength=filing_source.source_strength if filing_source else SourceStrength.A,
                    source_dimension=filing_source.dimension_id if filing_source else None,
                )
                for filing in filings
            ]
            self.cache.save_documents(filing_documents)
            documents.extend(filing_documents)

        documents.extend(self._optional_polygon_documents(target, documents, warnings))
        documents.extend(self._optional_news_documents(target, documents, warnings))

        return TargetIngestionResult(target=target, filings=filings, source_documents=documents, warnings=warnings)

    def _optional_polygon_documents(self, target, documents: list[SourceDocument], warnings: list[str]) -> list[SourceDocument]:
        selected = self.strategy.select("exchange_profile", include_disabled=True)
        polygon_source = next((source for source in selected if source.source_id == "polygon"), None)
        if not polygon_source:
            return []
        if not polygon_source.enabled:
            warnings.append(f"polygon disabled: {polygon_source.disabled_reason}")
            return []
        if any(document.source_id == "polygon" for document in documents):
            return []
        if not self.polygon_client:
            warnings.append("polygon disabled: adapter unavailable")
            return []

        document = self.polygon_client.fetch_ticker_profile(
            target,
            self.strategy.cache_ttl_hours("polygon"),
            source_strength=polygon_source.source_strength,
            source_dimension=polygon_source.dimension_id,
        )
        self.cache.save_document(document)
        return [document]

    def _optional_news_documents(self, target, documents: list[SourceDocument], warnings: list[str]) -> list[SourceDocument]:
        selected = self.strategy.select("recent_news_context", include_disabled=True)
        news_source = next((source for source in selected if source.source_id == "newsapi"), None)
        if not news_source:
            return []
        if not news_source.enabled:
            warnings.append(f"newsapi disabled: {news_source.disabled_reason}")
            return []
        if any(document.source_id == "newsapi" for document in documents):
            return []
        if not self.newsapi_client:
            warnings.append("newsapi disabled: adapter unavailable")
            return []

        news_documents = self.newsapi_client.fetch_target_articles(
            target,
            self.strategy.cache_ttl_hours("newsapi"),
            source_strength=news_source.source_strength,
            source_dimension=news_source.dimension_id,
            domains=news_source.config.retrieval.domains,
        )
        self.cache.save_documents(news_documents)
        return news_documents

    def _filings_from_documents(self, documents: list[SourceDocument]) -> list[FilingMetadata]:
        filings: list[FilingMetadata] = []
        for document in documents:
            if document.source_type != SourceType.sec_filing:
                continue
            filings.append(FilingMetadata.model_validate(document.metadata))
        return filings
