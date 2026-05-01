"""Declarative data-source selection for target resolution and ingestion."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import Settings
from src.domain import DataSourceConfig, DataSourcePolicy


@dataclass(frozen=True)
class SelectedDataSource:
    """A source selected for a use case, including disabled-state diagnostics."""

    source_id: str
    config: DataSourceConfig
    enabled: bool
    disabled_reason: str | None = None


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
        for source_id in self.policy.use_cases.get(use_case, []):
            config = self.policy.sources[source_id]
            enabled, reason = self._is_source_enabled(source_id, config)
            if enabled or include_disabled:
                selected.append(SelectedDataSource(source_id=source_id, config=config, enabled=enabled, disabled_reason=reason))
        return selected

    def source(self, source_id: str) -> DataSourceConfig:
        return self.policy.sources[source_id]

    def cache_ttl_hours(self, source_id: str) -> int:
        return self.policy.sources[source_id].cache_ttl_hours

    def _is_source_enabled(self, source_id: str, config: DataSourceConfig) -> tuple[bool, str | None]:
        if not config.enabled:
            return False, "disabled by data-source policy"

        if source_id == "edgar" and not self.settings.enable_sec_edgar:
            return False, "disabled by application settings"
        if source_id == "polygon" and not self.settings.polygon_api_key:
            return False, "missing BUG_POLYGON_API_KEY"
        if source_id == "newsapi" and not self.settings.news_api_key:
            return False, "missing BUG_NEWS_API_KEY"

        if config.api_key_env and not os.getenv(config.api_key_env) and not self._settings_key_present(config.api_key_env):
            return False, f"missing {config.api_key_env}"

        return True, None

    def _settings_key_present(self, api_key_env: str) -> bool:
        if api_key_env == "BUG_POLYGON_API_KEY":
            return bool(self.settings.polygon_api_key)
        if api_key_env == "BUG_NEWS_API_KEY":
            return bool(self.settings.news_api_key)
        return False


def load_data_source_policy(path: Path) -> DataSourcePolicy:
    with path.open("r", encoding="utf-8") as policy_file:
        raw_policy = yaml.safe_load(policy_file) or {}
    return DataSourcePolicy.model_validate(raw_policy)
