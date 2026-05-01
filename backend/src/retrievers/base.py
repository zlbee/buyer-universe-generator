"""Retriever interface for future strategic and financial candidate sources."""

from __future__ import annotations

from typing import Protocol

from src.domain import CandidateHit, TargetProfile


class CandidateRetriever(Protocol):
    """Generates raw candidate hits from one retrieval strategy."""

    name: str

    def retrieve(self, target_profile: TargetProfile) -> list[CandidateHit]:
        ...
