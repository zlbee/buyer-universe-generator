"""OpenRouter provider implementation for structured LLM responses."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src.config import Settings
from src.llm.client import LLMInteractionRecorder, LLMResponseError, MissingLLMConfigurationError

logger = logging.getLogger(__name__)


class OpenRouterProvider:
    """OpenRouter implementation using its OpenAI-compatible chat completions API."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        interaction_recorder: LLMInteractionRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds)
        self.interaction_recorder = interaction_recorder

    def generate_json(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        source_business_type: str = "unspecified",
    ) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            raise MissingLLMConfigurationError("BUG_OPENROUTER_API_KEY is required for structured LLM extraction")

        response_format: dict[str, Any] = {"type": "json_object"}
        provider_preferences: dict[str, Any] | None = None
        if json_schema:
            # Prefer provider-side schema enforcement when the selected model supports it.
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                },
            }
            # OpenRouter may otherwise route to providers that silently ignore response_format.
            provider_preferences = {"require_parameters": True}

        active_system_prompt = system_prompt or _default_json_system_prompt(schema_name)
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "response_format": response_format,
            "messages": [
                {
                    "role": "system",
                    "content": active_system_prompt,
                },
                {"role": "user", "content": prompt},
            ],
        }
        if provider_preferences:
            payload["provider"] = provider_preferences

        return self._send_structured_request(
            payload,
            schema_name,
            prompt,
            active_system_prompt,
            response_format["type"],
            source_business_type,
        )

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
        """Generate structured JSON while letting OpenRouter run its web-search server tool."""
        if not self.settings.openrouter_api_key:
            raise MissingLLMConfigurationError("BUG_OPENROUTER_API_KEY is required for structured LLM extraction")

        tool_parameters: dict[str, Any] = {
            "engine": search_engine,
            "max_results": max_results,
            "max_total_results": max_total_results,
            "search_context_size": search_context_size,
        }
        active_system_prompt = system_prompt or _default_json_system_prompt(schema_name)
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            # Web-search discovery uses JSON object mode to avoid provider-routing failures on models
            # that do not support strict JSON Schema response_format parameters.
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": active_system_prompt,
                },
                {"role": "user", "content": prompt},
            ],
            "tools": [{"type": "openrouter:web_search", "parameters": tool_parameters}],
        }

        return self._send_structured_request(
            payload,
            schema_name,
            prompt,
            active_system_prompt,
            "json_object:web_search",
            source_business_type,
        )

    def _send_structured_request(
        self,
        payload: dict[str, Any],
        schema_name: str,
        prompt: str,
        system_prompt: str,
        response_format_type: str,
        source_business_type: str,
    ) -> dict[str, Any]:
        response_payload: Any | None = None
        raw_output_content: str | None = None
        try:
            logger.info(
                "OpenRouter structured request started: schema=%s model=%s prompt_chars=%s response_format=%s",
                schema_name,
                self.settings.llm_model,
                len(prompt),
                response_format_type,
            )
            response = self.http_client.post(
                f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            logger.info(
                "OpenRouter structured request completed: schema=%s status_code=%s",
                schema_name,
                response.status_code,
            )
        except httpx.HTTPStatusError as error:
            raw_output_content = error.response.text
            logger.warning(
                "OpenRouter HTTP status error: schema=%s model=%s status_code=%s response_preview=%s",
                schema_name,
                self.settings.llm_model,
                error.response.status_code,
                _preview_text(error.response.text),
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=None,
                raw_output_content=raw_output_content,
                parsed_output=None,
                status="http_error",
                error_message=str(error),
            )
            raise LLMResponseError(f"OpenRouter request failed: {error}") from error
        except httpx.HTTPError as error:
            logger.warning(
                "OpenRouter HTTP transport error: schema=%s model=%s error_type=%s error=%s",
                schema_name,
                self.settings.llm_model,
                type(error).__name__,
                error,
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=None,
                raw_output_content=None,
                parsed_output=None,
                status="transport_error",
                error_message=str(error),
            )
            raise LLMResponseError(f"OpenRouter request failed: {error}") from error

        try:
            response_payload = response.json()
        except ValueError as error:
            raw_output_content = response.text
            logger.warning(
                "OpenRouter response body was not JSON: schema=%s status_code=%s response_preview=%s",
                schema_name,
                response.status_code,
                _preview_text(response.text),
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=None,
                raw_output_content=raw_output_content,
                parsed_output=None,
                status="invalid_response_json",
                error_message="OpenRouter response body was not valid JSON",
            )
            raise LLMResponseError("OpenRouter response body was not valid JSON") from error

        try:
            content = response_payload["choices"][0]["message"]["content"]
            raw_output_content = _content_to_log_text(content)
        except (KeyError, IndexError, TypeError) as error:
            logger.warning(
                "OpenRouter response missing message content: schema=%s payload_preview=%s",
                schema_name,
                _preview_json(response_payload),
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=response_payload,
                raw_output_content=None,
                parsed_output=None,
                status="missing_output_content",
                error_message="OpenRouter response did not contain choices[0].message.content",
            )
            raise LLMResponseError("OpenRouter response did not contain choices[0].message.content") from error

        try:
            parsed = _parse_structured_content(content, schema_name)
        except LLMResponseError as error:
            logger.warning(
                "OpenRouter response content parsing failed: schema=%s content_type=%s content_preview=%s",
                schema_name,
                type(content).__name__,
                _preview_json(content),
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=response_payload,
                raw_output_content=raw_output_content,
                parsed_output=None,
                status="parse_error",
                error_message=str(error),
            )
            raise

        if not isinstance(parsed, dict):
            logger.warning(
                "OpenRouter parsed JSON was not an object: schema=%s parsed_type=%s parsed_preview=%s",
                schema_name,
                type(parsed).__name__,
                _preview_json(parsed),
            )
            self._record_llm_interaction(
                source_business_type=source_business_type,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=payload,
                response_payload=response_payload,
                raw_output_content=raw_output_content,
                parsed_output=parsed,
                status="invalid_output_type",
                error_message="OpenRouter JSON response must be an object",
            )
            raise LLMResponseError("OpenRouter JSON response must be an object")

        logger.info(
            "OpenRouter structured response parsed: schema=%s top_level_keys=%s",
            schema_name,
            sorted(str(key) for key in parsed.keys()),
        )
        self._record_llm_interaction(
            source_business_type=source_business_type,
            schema_name=schema_name,
            response_format_type=response_format_type,
            prompt=prompt,
            system_prompt=system_prompt,
            request_payload=payload,
            response_payload=response_payload,
            raw_output_content=raw_output_content,
            parsed_output=parsed,
            status="success",
            error_message=None,
        )
        return parsed

    def _record_llm_interaction(
        self,
        *,
        source_business_type: str,
        schema_name: str,
        response_format_type: str,
        prompt: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        response_payload: Any | None,
        raw_output_content: str | None,
        parsed_output: Any | None,
        status: str,
        error_message: str | None,
    ) -> None:
        if not self.interaction_recorder:
            return

        try:
            self.interaction_recorder.record(
                source_business_type=source_business_type,
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload=request_payload,
                response_payload=response_payload,
                raw_output_content=raw_output_content,
                parsed_output=parsed_output,
                status=status,
                error_message=error_message,
            )
        except Exception:
            # Audit persistence should never mask the original LLM result or provider error.
            logger.exception(
                "Failed to persist LLM interaction: schema=%s business_type=%s status=%s",
                schema_name,
                source_business_type,
                status,
            )


def _default_json_system_prompt(schema_name: str) -> str:
    return f"Return only valid JSON for schema {schema_name}; do not include prose."


def _content_to_log_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _parse_structured_content(content: Any, schema_name: str) -> Any:
    if isinstance(content, dict):
        return _unwrap_schema_named_object(content, schema_name)

    if isinstance(content, list):
        content = _text_from_content_parts(content)

    if not isinstance(content, str):
        raise LLMResponseError("OpenRouter response content must be a JSON string or object")

    parsed = _load_json_from_text(content)
    return _unwrap_schema_named_object(parsed, schema_name)


def _text_from_content_parts(content_parts: list[Any]) -> str:
    text_parts: list[str] = []
    for part in content_parts:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"])

    if not text_parts:
        raise LLMResponseError("OpenRouter response content parts did not contain text")
    return "".join(text_parts)


def _load_json_from_text(content: str) -> Any:
    normalized = _strip_json_code_fence(content.strip())

    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        pass

    # Some providers still wrap the JSON in prose; recover the first JSON value.
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None
    for index, character in enumerate(normalized):
        if character not in "{[":
            continue
        try:
            parsed, _end_index = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError as error:
            last_error = error
            continue
        return parsed

    raise LLMResponseError("OpenRouter response content was not valid JSON") from last_error


def _strip_json_code_fence(content: str) -> str:
    lines = content.splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return content


def _unwrap_schema_named_object(parsed: Any, schema_name: str) -> Any:
    if isinstance(parsed, dict) and isinstance(parsed.get(schema_name), dict):
        return parsed[schema_name]
    return parsed


def _preview_json(value: Any, limit: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return _preview_text(text, limit)


def _preview_text(value: str, limit: int = 600) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."
