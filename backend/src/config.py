"""Application configuration loaded from defaults, .env, and environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _default_env_file() -> Path:
    source_root = Path(__file__).resolve().parents[1]
    if source_root.name == "backend":
        # Local checkout layout is <repo>/backend/src/config.py; keep one shared .env at <repo>/.env.
        return source_root.parent / ".env"
    # Container layout is /app/src/config.py; Compose injects env vars, but /app/.env remains the natural fallback.
    return source_root / ".env"


class Settings(BaseSettings):
    """Runtime settings shared by the API, CLI, and pipeline modules."""

    model_config = SettingsConfigDict(
        env_file=_default_env_file(),
        env_file_encoding="utf-8",
        env_prefix="BUG_",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Buyer Universe Generator"
    environment: str = "local"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/buyer_universe.db"
    cache_dir: Path = Path("./data/cache")
    retrieval_rules_path: Path = Field(
        default=Path("./config/retrieval_rules.yaml"),
        validation_alias=AliasChoices(
            "BUG_RETRIEVAL_RULES_PATH",
            "BUG_DATASOURCE_POLICY_PATH",
            "retrieval_rules_path",
            "datasource_policy_path",
        ),
    )
    keyword_taxonomy_path: Path = Path("./config/keyword_taxonomy.yaml")
    pe_seed_universe_path: Path = Path("./config/pe_seed_universe.yaml")

    edgar_identity: str = Field(
        default="buyer-universe-generator/0.1 contact@example.com",
        validation_alias=AliasChoices("BUG_EDGAR_IDENTITY", "EDGAR_IDENTITY"),
    )
    sec_user_agent: str = "buyer-universe-generator/0.1 contact@example.com"
    polygon_api_key: str | None = None
    news_api_key: str | None = None
    fmp_api_key: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_provider: str = "openrouter"
    llm_model: str = "openrouter/auto"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_input_chars: int = Field(default=120_000, ge=1_000)
    target_profile_extractor_version: str = "target-profile-v1"
    target_profile_cache_ttl_hours: int = Field(default=24, ge=0)
    buyer_recall_cache_ttl_hours: int = Field(default=24, ge=0)
    buyer_recall_cache_version: str = "buyer-recall-v1"
    enable_ir_page_discovery: bool = True
    ir_discovery_max_candidates: int = Field(default=5, ge=1, le=10)
    ir_discovery_search_engine: str = "auto"
    ir_discovery_search_context_size: str = "low"

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

    @field_validator("log_level")
    @classmethod
    def log_level_must_be_known(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError("log_level must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG, or NOTSET")
        return normalized

    @property
    def datasource_policy_path(self) -> Path:
        """Backward-compatible alias for the retrieval rules file path."""

        return self.retrieval_rules_path


@lru_cache
def get_settings() -> Settings:
    """Return cached settings so all modules see one consistent configuration."""

    return Settings()
