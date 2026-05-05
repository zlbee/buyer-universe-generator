"""Declarative data-source selection for target resolution and ingestion."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import Settings
from src.domain import DataSourceConfig, DataSourceDimensionConfig, DataSourcePolicy, DataSourceRetrieverConfig, SourceStrength


@dataclass(frozen=True)
class SelectedDataSource:
    """A source selected for a use case, including disabled-state diagnostics."""

    source_id: str
    dimension_id: str
    config: DataSourceConfig
    dimension: DataSourceDimensionConfig
    enabled: bool
    disabled_reason: str | None = None

    @property
    def source_strength(self) -> SourceStrength:
        return self.dimension.source_strength

    @property
    def field_coverage(self) -> list[str]:
        return self.dimension.field_coverage


class DataSourceStrategy:
    """Loads source policy once and answers which sources should be used."""

    def __init__(self, policy: DataSourcePolicy, settings: Settings) -> None:
        self.policy = policy
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "DataSourceStrategy":
        policy = load_data_source_policy(settings.datasource_policy_path)
        return cls(policy=policy, settings=settings)

    def select(self, use_case: str, include_disabled: bool = False) -> list[SelectedDataSource]:
        selected: list[SelectedDataSource] = []
        for source_reference in self.policy.use_cases.get(use_case, []):
            source_id = source_reference.source_id
            config = self.policy.sources[source_id]
            dimension = config.dimensions[source_reference.dimension]
            enabled, reason = self._is_source_enabled(source_id, config)
            if enabled or include_disabled:
                selected.append(
                    SelectedDataSource(
                        source_id=source_id,
                        dimension_id=source_reference.dimension,
                        config=config,
                        dimension=dimension,
                        enabled=enabled,
                        disabled_reason=reason,
                    )
                )
        return selected

    def source(self, source_id: str) -> DataSourceConfig:
        return self.policy.sources[source_id]

    def selected_source(self, use_case: str, source_id: str, include_disabled: bool = False) -> SelectedDataSource | None:
        return next(
            (source for source in self.select(use_case, include_disabled=include_disabled) if source.source_id == source_id),
            None,
        )

    def retriever_config(self, retriever_name: str) -> DataSourceRetrieverConfig | None:
        return self.policy.retrievers.get(retriever_name)

    def cache_ttl_hours(self, source_id: str) -> int:
        return self.policy.sources[source_id].cache_ttl_hours

    def _is_source_enabled(self, source_id: str, config: DataSourceConfig) -> tuple[bool, str | None]:
        if not config.enabled:
            return False, "disabled by data-source policy"

        if source_id == "edgar" and not self.settings.enable_sec_edgar:
            return False, "disabled by application settings"
        if source_id == "company_pages" and not self.settings.enable_company_pages:
            return False, "disabled by application settings"
        if source_id == "polygon" and not self.settings.polygon_api_key:
            return False, "missing BUG_POLYGON_API_KEY"
        if source_id == "newsapi" and not self.settings.news_api_key:
            return False, "missing BUG_NEWS_API_KEY"
        if source_id == "fmp" and not self.settings.fmp_api_key:
            return False, "missing BUG_FMP_API_KEY"
        if source_id == "openrouter_web_search" and not self.settings.openrouter_api_key:
            return False, "missing BUG_OPENROUTER_API_KEY"

        if config.api_key_env and not os.getenv(config.api_key_env) and not self._settings_key_present(config.api_key_env):
            return False, f"missing {config.api_key_env}"

        return True, None

    def _settings_key_present(self, api_key_env: str) -> bool:
        if api_key_env == "BUG_POLYGON_API_KEY":
            return bool(self.settings.polygon_api_key)
        if api_key_env == "BUG_NEWS_API_KEY":
            return bool(self.settings.news_api_key)
        if api_key_env == "BUG_FMP_API_KEY":
            return bool(self.settings.fmp_api_key)
        if api_key_env == "BUG_OPENROUTER_API_KEY":
            return bool(self.settings.openrouter_api_key)
        return False


def load_data_source_policy(path: Path) -> DataSourcePolicy:
    with path.open("r", encoding="utf-8") as policy_file:
        raw_policy = yaml.safe_load(policy_file) or {}
    return DataSourcePolicy.model_validate(raw_policy)
