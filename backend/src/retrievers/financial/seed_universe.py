"""Config-driven private-equity seed universe for Phase 5 retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.retrievers.strategic.common import normalize_entity_key, unique_terms


class PESeedFirm(BaseModel):
    """One configured PE sponsor identity used to recognize FMP acquirers."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    domain: str | None = None

    @field_validator("canonical_name", mode="before")
    @classmethod
    def normalize_canonical_name(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("domain")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        return unique_terms(value)

    @property
    def match_names(self) -> list[str]:
        return unique_terms([self.canonical_name, *self.aliases])


class PESeedUniverse(BaseModel):
    """Validated PE sponsor seed file."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    firms: list[PESeedFirm] = Field(default_factory=list)


@dataclass(frozen=True)
class PESeedMatch:
    firm: PESeedFirm
    matched_name: str


class PESeedMatcher:
    """Exact normalized-name matcher over canonical firm names and aliases."""

    def __init__(self, universe: PESeedUniverse) -> None:
        self._matches: dict[str, PESeedMatch] = {}
        for firm in universe.firms:
            for name in firm.match_names:
                key = normalize_entity_key(name)
                if key and key not in self._matches:
                    self._matches[key] = PESeedMatch(firm=firm, matched_name=name)

    def match(self, value: str | None) -> PESeedMatch | None:
        if not value:
            return None
        return self._matches.get(normalize_entity_key(value))


def load_pe_seed_universe(path: Path) -> PESeedUniverse:
    """Load and validate the configured PE seed universe."""

    with path.open("r", encoding="utf-8") as seed_file:
        raw_seed = yaml.safe_load(seed_file) or {}
    return PESeedUniverse.model_validate(raw_seed)
