"""OpenRouter provider implementation for structured LLM responses."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src.config import Settings
from src.llm.client import LLMResponseError, MissingLLMConfigurationError

logger = logging.getLogger(__name__)


class OpenRouterProvider:
    """OpenRouter implementation using its OpenAI-compatible chat completions API."""

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http_client = http_client or httpx.Client(timeout=settings.request_timeout_seconds)

    def generate_json(
        self,
        prompt: str,
        schema_name: str,
        json_schema: dict[str, Any] | None = None,
        system_prompt: str | None = None,
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

        try:
            payload: dict[str, Any] = {
                "model": self.settings.llm_model,
                "response_format": response_format,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt or _default_json_system_prompt(schema_name),
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            if provider_preferences:
                payload["provider"] = provider_preferences

            logger.info(
                "OpenRouter structured request started: schema=%s model=%s prompt_chars=%s response_format=%s",
                schema_name,
                self.settings.llm_model,
                len(prompt),
                response_format["type"],
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
            logger.warning(
                "OpenRouter HTTP status error: schema=%s model=%s status_code=%s response_preview=%s",
                schema_name,
                self.settings.llm_model,
                error.response.status_code,
                _preview_text(error.response.text),
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
            raise LLMResponseError(f"OpenRouter request failed: {error}") from error

        try:
            response_payload = response.json()
        except ValueError as error:
            logger.warning(
                "OpenRouter response body was not JSON: schema=%s status_code=%s response_preview=%s",
                schema_name,
                response.status_code,
                _preview_text(response.text),
            )
            raise LLMResponseError("OpenRouter response body was not valid JSON") from error

        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            logger.warning(
                "OpenRouter response missing message content: schema=%s payload_preview=%s",
                schema_name,
                _preview_json(response_payload),
            )
            raise LLMResponseError("OpenRouter response did not contain choices[0].message.content") from error

        try:
            parsed = _parse_structured_content(content, schema_name)
        except LLMResponseError:
            logger.warning(
                "OpenRouter response content parsing failed: schema=%s content_type=%s content_preview=%s",
                schema_name,
                type(content).__name__,
                _preview_json(content),
            )
            raise

        if not isinstance(parsed, dict):
            logger.warning(
                "OpenRouter parsed JSON was not an object: schema=%s parsed_type=%s parsed_preview=%s",
                schema_name,
                type(parsed).__name__,
                _preview_json(parsed),
            )
            raise LLMResponseError("OpenRouter JSON response must be an object")

        logger.info(
            "OpenRouter structured response parsed: schema=%s top_level_keys=%s",
            schema_name,
            sorted(str(key) for key in parsed.keys()),
        )
        return parsed


def _default_json_system_prompt(schema_name: str) -> str:
    return f"Return only valid JSON for schema {schema_name}; do not include prose."


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
