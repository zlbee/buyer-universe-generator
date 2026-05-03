"""SQLite-backed audit log for raw LLM provider interactions."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from src.repositories.models import LLMInteractionRecord


class LLMInteractionLog:
    """Persists provider request/response payloads for later prompt review."""

    def __init__(self, session: Session) -> None:
        self.session = session

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
        self.session.add(
            LLMInteractionRecord(
                source_business_type=source_business_type,
                provider=provider,
                model=model,
                schema_name=schema_name,
                response_format_type=response_format_type,
                prompt=prompt,
                system_prompt=system_prompt,
                request_payload_json=_dump_json(request_payload),
                response_payload_json=_dump_json(response_payload) if response_payload is not None else None,
                raw_output_content=raw_output_content,
                parsed_output_json=_dump_json(parsed_output) if parsed_output is not None else None,
                status=status,
                error_message=error_message,
            )
        )
        self.session.commit()


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
