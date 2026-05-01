"""LLM client interface used for extraction and classification only."""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    """Produces structured outputs that must be backed by evidence before promotion."""

    def generate_json(self, prompt: str, schema_name: str) -> dict[str, Any]:
        ...

