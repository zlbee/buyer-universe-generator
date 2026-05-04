"""Phase 4 strategic retriever fan-out."""

from __future__ import annotations

from typing import Any

from src.domain import CandidateHit, StrategicRetrievalResult, TargetProfile


class BuyerCandidateRetriever:
    """Fans out Phase 4 strategic retrievers and preserves raw OR-recall hits."""

    def __init__(self, retrievers: list[Any]) -> None:
        self.retrievers = retrievers

    def retrieve(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        hits: list[CandidateHit] = []
        warnings: list[str] = []
        retriever_metadata: list[dict[str, Any]] = []

        for retriever in self.retrievers:
            name = getattr(retriever, "name", type(retriever).__name__)
            try:
                if hasattr(retriever, "retrieve_with_context"):
                    result = retriever.retrieve_with_context(target_profile)
                else:
                    result = StrategicRetrievalResult(hits=retriever.retrieve(target_profile))
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

        return StrategicRetrievalResult(
            hits=hits,
            warnings=warnings,
            metadata={"retrievers": retriever_metadata, "hit_count": len(hits)},
        )
