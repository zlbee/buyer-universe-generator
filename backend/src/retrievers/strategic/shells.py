"""No-op strategic retriever shells planned for later Phase 4 expansion."""

from __future__ import annotations

from typing import Any

from src.domain import CandidateHit, StrategicRetrievalResult, TargetProfile


class _StrategicRetrieverShell:
    """Base shell for planned strategic retrievers that are intentionally no-op in this phase."""

    name = "StrategicRetrieverShell"

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def retrieve(self, _target_profile: TargetProfile) -> list[CandidateHit]:
        # TODO: Implement this retrieval path after Same SIC and M&A-history recall are stable.
        return []

    def retrieve_with_context(self, _target_profile: TargetProfile) -> StrategicRetrievalResult:
        return StrategicRetrievalResult(metadata={"retriever": self.name, "status": "not_implemented"})


class AdjacentIndustryRetriever(_StrategicRetrieverShell):
    """Placeholder for adjacent industry recall."""

    name = "AdjacentIndustryRetriever"


class BusinessSimilarityRetriever(_StrategicRetrieverShell):
    """Placeholder for BM25/TF-IDF business-description similarity recall."""

    name = "BusinessSimilarityRetriever"


class ProductCustomerChannelRetriever(_StrategicRetrieverShell):
    """Placeholder for product, customer, and channel overlap recall."""

    name = "ProductCustomerChannelRetriever"


class PeerCompanyRetriever(_StrategicRetrieverShell):
    """Placeholder for public peer-company recall."""

    name = "PeerCompanyRetriever"


class SupplyChainRetriever(_StrategicRetrieverShell):
    """Placeholder for upstream, downstream, and vertical-integration recall."""

    name = "SupplyChainRetriever"
