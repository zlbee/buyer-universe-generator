"""Declarative retrieval-rule selection for target profiling and buyer recall."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import Settings
from src.domain import (
    RetrievalEvidenceProfile,
    RetrievalProviderConfig,
    RetrievalRetrieverConfig,
    RetrievalRules,
    SourceStrength,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectedDataSource:
    """A source selected for a use case, including disabled-state diagnostics."""

    source_id: str
    dimension_id: str
    config: RetrievalProviderConfig
    dimension: RetrievalEvidenceProfile
    enabled: bool
    disabled_reason: str | None = None

    @property
    def source_strength(self) -> SourceStrength:
        return self.dimension.source_strength


class DataSourceStrategy:
    """Loads retrieval rules once and answers which sources should be used."""

    def __init__(self, policy: RetrievalRules, settings: Settings) -> None:
        self.policy = policy
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "DataSourceStrategy":
        policy = load_retrieval_rules(settings.retrieval_rules_path)
        return cls(policy=policy, settings=settings)

    def select(self, use_case: str, include_disabled: bool = False) -> list[SelectedDataSource]:
        selected: list[SelectedDataSource] = []
        for source_reference in self.policy.use_cases.get(use_case, []):
            source_id = source_reference.source_id
            config = self.policy.providers[source_id]
            dimension = self.policy.evidence_profiles[source_id][source_reference.dimension]
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

    def source(self, source_id: str) -> RetrievalProviderConfig:
        return self.policy.providers[source_id]

    def selected_source(self, use_case: str, source_id: str, include_disabled: bool = False) -> SelectedDataSource | None:
        return next(
            (source for source in self.select(use_case, include_disabled=include_disabled) if source.source_id == source_id),
            None,
        )

    def retriever_config(self, retriever_name: str) -> RetrievalRetrieverConfig | None:
        return self.policy.retrievers.get(retriever_name)

    def cache_ttl_hours(self, source_id: str) -> int:
        return self.policy.providers[source_id].cache_ttl_hours

    def _is_source_enabled(self, source_id: str, config: RetrievalProviderConfig) -> tuple[bool, str | None]:
        if not config.enabled:
            return False, "disabled by retrieval rules"

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
        if config.provider == "openrouter.ai" and not self.settings.openrouter_api_key:
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


def load_retrieval_rules(path: Path) -> RetrievalRules:
    effective_path = _existing_rules_path(path)
    with effective_path.open("r", encoding="utf-8") as rules_file:
        raw_rules = yaml.safe_load(rules_file) or {}
    if "providers" not in raw_rules and "sources" in raw_rules:
        logger.warning("Loading legacy datasources.yaml schema; migrate to retrieval_rules.yaml")
        raw_rules = _legacy_datasource_policy_to_retrieval_rules(raw_rules)
    return RetrievalRules.model_validate(raw_rules)


def load_data_source_policy(path: Path) -> RetrievalRules:
    """Backward-compatible loader alias for callers using the legacy name."""

    return load_retrieval_rules(path)


def _existing_rules_path(path: Path) -> Path:
    if path.exists():
        return path
    legacy_path = path.with_name("datasources.yaml")
    if path.name == "retrieval_rules.yaml" and legacy_path.exists():
        logger.warning("retrieval_rules.yaml not found; falling back to legacy datasources.yaml")
        return legacy_path
    new_path = path.with_name("retrieval_rules.yaml")
    if path.name == "datasources.yaml" and new_path.exists():
        logger.warning("legacy datasources.yaml path requested; using retrieval_rules.yaml")
        return new_path
    return path


def _legacy_datasource_policy_to_retrieval_rules(raw_policy: dict) -> dict:
    providers: dict[str, dict] = {}
    evidence_profiles: dict[str, dict[str, dict[str, str]]] = {}
    for source_id, source_config in (raw_policy.get("sources") or {}).items():
        providers[source_id] = {
            key: source_config[key]
            for key in ("provider", "enabled", "required", "api_key_env", "cache_ttl_hours")
            if key in source_config
        }
        if source_config.get("retrieval"):
            providers[source_id]["retrieval"] = source_config["retrieval"]
        evidence_profiles[source_id] = {
            dimension_id: {"source_strength": dimension_config["source_strength"]}
            for dimension_id, dimension_config in (source_config.get("dimensions") or {}).items()
            if "source_strength" in dimension_config
        }

    target_profile_use_cases: dict[str, list[dict]] = {}
    buyer_recall_use_cases: dict[str, list[dict]] = {}
    for use_case, selections in (raw_policy.get("use_cases") or {}).items():
        target = buyer_recall_use_cases if use_case.startswith("buyer_recall_") else target_profile_use_cases
        target[use_case] = selections

    return {
        "version": raw_policy.get("version", 1),
        "providers": providers,
        "evidence_profiles": evidence_profiles,
        "stages": {
            "target_profile_builder": {"use_cases": target_profile_use_cases},
            "potential_buyer_recaller": {
                "use_cases": buyer_recall_use_cases,
                "retrievers": raw_policy.get("retrievers") or {},
            },
        },
    }
