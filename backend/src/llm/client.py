"""LLM provider interfaces and shared errors."""

from __future__ import annotations

from typing import Any, Protocol


class LLMInteractionRecorder(Protocol):
    """Persists raw provider interactions without coupling callers to storage details."""

    def record(
        self,
        *,
        source_business_type: str,
        provider: str,
        model: str,
        schema_name: str,
        response_format_type: str,
        prompt: str,
        system_prompt: str | None,
        request_payload: dict[str, Any],
        response_payload: Any | None,
        raw_output_content: str | None,
        parsed_output: Any | None,
        status: str,
        error_message: str | None = None,
    ) -> None:
        ...


class LLMClient(Protocol):
    """Produces structured JSON outputs for business-layer extraction workflows."""

    def generate_json(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        source_business_type: str = "unspecified",
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
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        ...


class MissingLLMConfigurationError(RuntimeError):
    """Raised when a required LLM provider setting is unavailable."""


class LLMResponseError(RuntimeError):
    """Raised when the LLM provider returns a malformed structured response."""
