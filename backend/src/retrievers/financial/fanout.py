"""Financial buyer retriever fan-out."""

from __future__ import annotations

from typing import Any

from src.domain import CandidateHit, StrategicRetrievalResult, TargetProfile
from src.repositories.buyer_recall_cache import BuyerRecallCache


DEFAULT_FINANCIAL_BUYER_RECALL_STAGE = "financial_buyer_recall"


class FinancialBuyerCandidateRetriever:
    """Fans out Phase 5 financial retrievers and preserves raw OR-recall hits."""

    def __init__(
        self,
        retrievers: list[Any],
        *,
        cache: BuyerRecallCache | None = None,
        cache_ttl_hours: int = 24,
        stage_name: str = DEFAULT_FINANCIAL_BUYER_RECALL_STAGE,
        stage_version: str = "buyer-recall-v2",
    ) -> None:
        self.retrievers = retrievers
        self.cache = cache
        self.cache_ttl_hours = cache_ttl_hours
        self.stage_name = stage_name
        self.stage_version = stage_version

    def retrieve(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        if self.cache:
            cached = self.cache.get_valid(target_profile, self.stage_name, self.stage_version)
            if cached:
                return _with_cache_metadata(cached.result, cached.metadata)

        hits: list[CandidateHit] = []
        warnings: list[str] = []
        retriever_metadata: list[dict[str, Any]] = []

        for retriever in self.retrievers:
            name = getattr(retriever, "name", type(retriever).__name__)
            try:
                if hasattr(retriever, "retrieve_with_context"):
                    result = retriever.retrieve_with_context(target_profile)
                else:
                    raw_result = retriever.retrieve(target_profile)
                    result = raw_result if isinstance(raw_result, StrategicRetrievalResult) else StrategicRetrievalResult(hits=raw_result)
            except Exception as error:
                warnings.append(f"{name} failed: {type(error).__name__}: {error}")
                retriever_metadata.append({"name": name, "status": "failed", "hit_count": 0})
                continue

            hits.extend(result.hits)
            warnings.extend(result.warnings)
            retriever_metadata.append(
                {
                    "name": name,
                    "status": "completed",
                    "hit_count": len(result.hits),
                    "metadata": result.metadata,
                }
            )

        result = StrategicRetrievalResult(
            hits=hits,
            warnings=warnings,
            metadata={"retrievers": retriever_metadata, "hit_count": len(hits)},
        )
        if not self.cache:
            return result
        if _has_retriever_failures(result):
            return _with_cache_metadata(result, {"status": "skipped", "reason": "retriever_failure", "stage_name": self.stage_name})
        cache_metadata = self.cache.save(
            target_profile,
            self.stage_name,
            self.stage_version,
            result,
            self.cache_ttl_hours,
        )
        return _with_cache_metadata(result, cache_metadata)


def _has_retriever_failures(result: StrategicRetrievalResult) -> bool:
    # Do not persist partial recall output when an exception may have suppressed viable candidates.
    return any(retriever.get("status") == "failed" for retriever in result.metadata.get("retrievers", []))


def _with_cache_metadata(result: StrategicRetrievalResult, cache_metadata: dict[str, Any]) -> StrategicRetrievalResult:
    metadata = {**result.metadata, "cache": cache_metadata}
    return result.model_copy(update={"metadata": metadata})
