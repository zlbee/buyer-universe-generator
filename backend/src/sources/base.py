"""Source client interfaces used by concrete public-data adapters."""

from __future__ import annotations

from typing import Protocol


class SourceClient(Protocol):
    """Fetches raw source material and returns text or structured metadata."""

    def fetch(self, identifier: str) -> str:
        ...

