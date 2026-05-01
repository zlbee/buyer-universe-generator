"""Application configuration loaded from defaults, .env, and environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the API, CLI, and pipeline modules."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BUG_",
        extra="ignore",
    )

    app_name: str = "Buyer Universe Generator"
    environment: str = "local"
    debug: bool = False

    database_url: str = "sqlite:///./data/buyer_universe.db"
    cache_dir: Path = Path("./data/cache")

    sec_user_agent: str = "buyer-universe-generator/0.1 contact@example.com"
    openrouter_api_key: str | None = None
    llm_model: str = "openrouter/auto"

    enable_sec_edgar: bool = True
    enable_company_pages: bool = True
    enable_news_api: bool = False
    enable_wikidata: bool = True
    enable_open_web_discovery: bool = False

    max_strategic_candidates: int = Field(default=150, ge=1)
    max_financial_candidates: int = Field(default=100, ge=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    cache_ttl_hours: int = Field(default=24, ge=0)
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    strategic_scoring_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "strategic_fit": 0.30,
            "mna_history": 0.25,
            "financial_capacity": 0.20,
            "geographic_fit": 0.10,
            "evidence_strength": 0.10,
            "data_confidence": 0.05,
        }
    )
    financial_scoring_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "sector_focus": 0.30,
            "portfolio_fit": 0.25,
            "deal_activity": 0.20,
            "size_fit": 0.15,
            "evidence_strength": 0.10,
        }
    )

    @field_validator("database_url")
    @classmethod
    def database_url_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("database_url must not be empty")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return cached settings so all modules see one consistent configuration."""

    return Settings()
