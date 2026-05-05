"""Factory helpers for Phase 1 services."""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.config import Settings
from src.llm import LLMClient, OpenRouterProvider
from src.pipelines.source_ingestion import SourceIngestionService
from src.pipelines.target_profile_extraction import TargetProfileExtractor
from src.pipelines.target_resolution import TargetResolver
from src.repositories.data_source_audit_log import DataSourceAuditLog
from src.repositories.llm_interaction_log import LLMInteractionLog
from src.repositories.source_cache import SourceCache
from src.repositories.target_profile_cache import TargetProfileCache
from src.retrievers import (
    AdjacentIndustryRetriever,
    BusinessSimilarityRetriever,
    BuyerCandidateRetriever,
    FinancialBuyerCandidateRetriever,
    MAHistoryRetriever,
    PeerCompanyRetriever,
    PEDealActivityRetriever,
    ProductCustomerChannelRetriever,
    SameSicRetriever,
    SupplyChainRetriever,
)
from src.sources.investor_relations import InvestorRelationsPageDiscovery
from src.sources.registry import SourceRegistry
from src.sources.strategy import DataSourceStrategy


def build_source_ingestion_service(settings: Settings, session: Session) -> SourceIngestionService:
    strategy = DataSourceStrategy.from_settings(settings)
    request_recorder = DataSourceAuditLog(session)
    registry = SourceRegistry(settings, strategy=strategy, request_recorder=request_recorder)
    cache = SourceCache(session)
    edgar_client = registry.edgar()
    resolver = TargetResolver(strategy=strategy, edgar_client=edgar_client, source_cache=cache)
    return SourceIngestionService(
        strategy=strategy,
        resolver=resolver,
        cache=cache,
        edgar_client=edgar_client,
        polygon_client=registry.polygon(),
        newsapi_client=registry.newsapi(),
    )


def build_target_profile_extractor(
    settings: Settings,
    session: Session,
    llm_client: LLMClient | None = None,
) -> TargetProfileExtractor:
    strategy = DataSourceStrategy.from_settings(settings)
    request_recorder = DataSourceAuditLog(session)
    registry = SourceRegistry(settings, strategy=strategy, request_recorder=request_recorder)
    cache = SourceCache(session)
    edgar_client = registry.edgar()
    resolver = TargetResolver(strategy=strategy, edgar_client=edgar_client, source_cache=cache)
    ingestion_service = SourceIngestionService(
        strategy=strategy,
        resolver=resolver,
        cache=cache,
        edgar_client=edgar_client,
        polygon_client=registry.polygon(),
        newsapi_client=registry.newsapi(),
    )
    provider = llm_client or OpenRouterProvider(settings, interaction_recorder=LLMInteractionLog(session))
    ir_page_discovery = (
        InvestorRelationsPageDiscovery(settings, provider, request_recorder=request_recorder)
        if settings.enable_ir_page_discovery and hasattr(provider, "generate_json_with_web_search")
        else None
    )
    return TargetProfileExtractor(
        settings=settings,
        strategy=strategy,
        ingestion_service=ingestion_service,
        source_cache=cache,
        profile_cache=TargetProfileCache(session),
        edgar_client=edgar_client,
        llm_client=provider,
        ir_page_discovery=ir_page_discovery,
    )


def build_strategic_buyer_candidate_retriever(settings: Settings, session: Session) -> BuyerCandidateRetriever:
    """Build the Phase 4 strategic first-pass retriever fan-out."""

    strategy = DataSourceStrategy.from_settings(settings)
    request_recorder = DataSourceAuditLog(session)
    registry = SourceRegistry(settings, strategy=strategy, request_recorder=request_recorder)
    edgar_client = registry.edgar()
    llm_provider = OpenRouterProvider(settings, interaction_recorder=LLMInteractionLog(session))
    return BuyerCandidateRetriever(
        [
            SameSicRetriever(strategy, edgar_client),
            AdjacentIndustryRetriever(strategy),
            BusinessSimilarityRetriever(strategy),
            ProductCustomerChannelRetriever(strategy),
            PeerCompanyRetriever(strategy),
            MAHistoryRetriever(
                strategy,
                newsapi_client=registry.newsapi(),
                google_news_rss_client=registry.google_news_rss(),
                edgar_client=edgar_client,
                buyer_identity_resolver=edgar_client,
                llm_client=llm_provider,
            ),
            SupplyChainRetriever(strategy),
        ]
    )


def build_financial_buyer_candidate_retriever(settings: Settings, session: Session) -> FinancialBuyerCandidateRetriever:
    """Build the Phase 5 financial first-pass retriever fan-out."""

    strategy = DataSourceStrategy.from_settings(settings)
    request_recorder = DataSourceAuditLog(session)
    registry = SourceRegistry(settings, strategy=strategy, request_recorder=request_recorder)
    llm_provider = OpenRouterProvider(settings, interaction_recorder=LLMInteractionLog(session))
    return FinancialBuyerCandidateRetriever(
        [
            PEDealActivityRetriever(
                strategy,
                fmp_client=registry.fmp(),
                web_search_client=llm_provider,
            ),
        ]
    )
