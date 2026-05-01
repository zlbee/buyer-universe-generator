"""Exporter interface for JSON, CSV, and human-readable outputs."""

from __future__ import annotations

from typing import Protocol

from src.domain import LongListCandidate


class CandidateExporter(Protocol):
    """Serializes long-list candidates into a concrete output format."""

    def export(self, candidates: list[LongListCandidate]) -> str:
        ...
