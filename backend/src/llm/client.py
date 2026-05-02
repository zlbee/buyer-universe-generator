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


class MissingLLMConfigurationError(RuntimeError):
    """Raised when a required LLM provider setting is unavailable."""


class LLMResponseError(RuntimeError):
    """Raised when the LLM provider returns a malformed structured response."""
