"""Pipeline orchestration helpers for progressively implemented phases."""

from __future__ import annotations

from typing import Any

from src.domain import CandidateHit, StrategicRetrievalResult, TargetProfile


class PipelineOrchestrator:
    """Coordinates target extraction and first-pass candidate retrieval phases."""

    def __init__(self, strategic_candidate_retriever: Any | None = None) -> None:
        self.phase = "strategic_candidate_retrieval"
        self.strategic_candidate_retriever = strategic_candidate_retriever

    def retrieve_strategic_candidates(self, target_profile: TargetProfile) -> StrategicRetrievalResult:
        """Run multi-path strategic buyer recall without dedupe, filtering, or scoring."""

        if not self.strategic_candidate_retriever:
            return StrategicRetrievalResult(metadata={"status": "no_strategic_retriever_configured"})
        result = self.strategic_candidate_retriever.retrieve(target_profile)
        if isinstance(result, StrategicRetrievalResult):
            return result
        # This keeps the orchestrator tolerant of simple CandidateRetriever-style fan-outs in tests.
        return StrategicRetrievalResult(hits=_coerce_hits(result))


def _coerce_hits(value: Any) -> list[CandidateHit]:
    if isinstance(value, list):
        return [hit for hit in value if isinstance(hit, CandidateHit)]
    return []
