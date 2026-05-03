"""LLM provider interfaces and shared errors."""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    """Produces structured JSON outputs for business-layer extraction workflows."""

    def generate_json(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        ...


class WebSearchJSONClient(Protocol):
    """Produces structured JSON outputs with provider-managed web search enabled."""

    def generate_json_with_web_search(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_results: int = 5,
        max_total_results: int = 5,
        search_engine: str = "auto",
        search_context_size: str = "low",
    ) -> dict[str, Any]:
        ...


class MissingLLMConfigurationError(RuntimeError):
    """Raised when a required LLM provider setting is unavailable."""


class LLMResponseError(RuntimeError):
    """Raised when the LLM provider returns a malformed structured response."""
