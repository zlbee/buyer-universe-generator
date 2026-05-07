"""Declarative retrieval-rule selection for target profiling and buyer recall."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.config import Settings
from src.domain import (
    RetrievalProviderConfig,
    RetrievalRetrieverConfig,
    RetrievalRules,
    SourceStrength,
)


logger = logging.getLogger(__name__)


# Keep old YAML use-case names readable while active code and config use the
# shorter v5 names. This lets older rule files still route through the pipeline.
_USE_CASE_ALIASES = {
    "target_resolution": "target_identity",
    "target_identity": "target_resolution",
    "sec_filings": "sec_filing_metadata",
    "sec_filing_metadata": "sec_filings",
    "seller_profile_business_description": "business_description",
    "business_description": "seller_profile_business_description",
    "seller_profile_company_strategy": "company_strategy",
    "company_strategy": "seller_profile_company_strategy",
    "news_discovery": "recent_news_context",
    "recent_news_context": "news_discovery",
    "buyer_recall_strategic_public_companies": "public_company_peer_discovery",
    "public_company_peer_discovery": "buyer_recall_strategic_public_companies",
    "buyer_recall_strategic_intent": "strategic_acquisition_intent",
    "strategic_acquisition_intent": "buyer_recall_strategic_intent",
    "buyer_recall_transaction_signals": "strategic_transaction_activity",
    "strategic_transaction_activity": "buyer_recall_transaction_signals",
    "buyer_recall_financial_sponsors": "financial_sponsor_activity",
    "financial_sponsor_activity": "buyer_recall_financial_sponsors",
}


@dataclass(frozen=True)
class SelectedDataSource:
    """A source selected for a use case, including disabled-state diagnostics."""

    source_id: str
    dimension_id: str
    config: RetrievalProviderConfig
    source_strength: SourceStrength
    role: str | None
    priority: int | None
    enabled: bool
    disabled_reason: str | None = None


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
        for source_reference in self._source_references(use_case):
            source_id = source_reference.source_id
            config = self.policy.providers[source_id]
            enabled, reason = self._is_source_enabled(source_id, config)
            if enabled or include_disabled:
                selected.append(
                    SelectedDataSource(
                        source_id=source_id,
                        dimension_id=source_reference.dimension,
                        config=config,
                        source_strength=source_reference.source_strength,
                        role=source_reference.role,
                        priority=source_reference.priority,
                        enabled=enabled,
                        disabled_reason=reason,
                    )
                )
        return selected

    def _source_references(self, use_case: str):
        references = self.policy.use_cases.get(use_case)
        if references is not None:
            return references
        alias = _USE_CASE_ALIASES.get(use_case)
        return self.policy.use_cases.get(alias, []) if alias else []

    def source(self, source_id: str) -> RetrievalProviderConfig:
        return self.policy.providers[source_id]

    def selected_source(self, use_case: str, source_id: str, include_disabled: bool = False) -> SelectedDataSource | None:
        return next(
            (source for source in self.select(use_case, include_disabled=include_disabled) if source.source_id == source_id),
            None,
        )

    def selected_source_by_role(self, use_case: str, role: str, include_disabled: bool = False) -> SelectedDataSource | None:
        return next(
            (source for source in self.select(use_case, include_disabled=include_disabled) if source.role == role),
            None,
        )

    def source_role_map(self, use_case: str, include_disabled: bool = False) -> dict[str, str]:
        return {
            source.role: source.source_id
            for source in self.select(use_case, include_disabled=include_disabled)
            if source.role is not None
        }

    def selected_sources_by_priority(self, use_case: str, include_disabled: bool = False) -> list[SelectedDataSource]:
        indexed_sources = list(enumerate(self.select(use_case, include_disabled=include_disabled)))
        ordered = sorted(
            indexed_sources,
            key=lambda item: (
                item[1].priority is None,
                item[1].priority if item[1].priority is not None else item[0],
                item[0],
            ),
        )
        return [source for _index, source in ordered]

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
    if "evidence_profiles" in raw_rules:
        logger.warning("Loading legacy evidence_profiles schema; inline source_strength under stages")
        raw_rules = _inline_legacy_evidence_profiles(raw_rules)
    if _has_legacy_retriever_source_bindings(raw_rules):
        logger.warning("Loading legacy retriever source bindings; inline roles and priorities under stages")
        raw_rules = _inline_legacy_retriever_source_bindings(raw_rules)
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
        target[use_case] = [_selection_with_legacy_source_strength(selection, evidence_profiles) for selection in selections]

    return {
        "version": raw_policy.get("version", 1),
        "providers": providers,
        "stages": {
            "target_profile_builder": {"use_cases": target_profile_use_cases},
            "potential_buyer_recaller": {
                "use_cases": buyer_recall_use_cases,
                "retrievers": raw_policy.get("retrievers") or {},
            },
        },
    }


def _inline_legacy_evidence_profiles(raw_rules: dict) -> dict:
    evidence_profiles = raw_rules.get("evidence_profiles") or {}
    stages: dict[str, dict] = {}
    for stage_name, stage_config in (raw_rules.get("stages") or {}).items():
        stage_copy = {key: value for key, value in stage_config.items() if key != "use_cases"}
        stage_copy["use_cases"] = {
            use_case: [_selection_with_legacy_source_strength(selection, evidence_profiles) for selection in selections]
            for use_case, selections in (stage_config.get("use_cases") or {}).items()
        }
        stages[stage_name] = stage_copy

    return {
        key: value
        for key, value in {
            **raw_rules,
            "stages": stages,
            "evidence_profiles": None,
        }.items()
        if key != "evidence_profiles"
    }


def _selection_with_legacy_source_strength(selection: dict, evidence_profiles: dict) -> dict:
    if "source_strength" in selection:
        return dict(selection)

    source_id = selection.get("source_id")
    dimension = selection.get("dimension")
    profile = (evidence_profiles.get(source_id) or {}).get(dimension) or {}
    if "source_strength" not in profile:
        return dict(selection)

    # Legacy configs stored evidence strength outside the stage selection; the
    # active schema keeps each use-case source selection self-contained.
    return {**selection, "source_strength": profile["source_strength"]}


def _has_legacy_retriever_source_bindings(raw_rules: dict) -> bool:
    for stage_config in (raw_rules.get("stages") or {}).values():
        for retriever_config in (stage_config.get("retrievers") or {}).values():
            if "source_roles" in retriever_config or "source_priority" in retriever_config:
                return True
    return False


def _inline_legacy_retriever_source_bindings(raw_rules: dict) -> dict:
    stages: dict[str, dict] = {}
    for stage_name, stage_config in (raw_rules.get("stages") or {}).items():
        stage_copy = dict(stage_config)
        use_cases = {
            use_case: [dict(selection) for selection in selections]
            for use_case, selections in (stage_config.get("use_cases") or {}).items()
        }
        retrievers: dict[str, dict] = {}
        for retriever_name, retriever_config in (stage_config.get("retrievers") or {}).items():
            retriever_copy = {
                key: value
                for key, value in retriever_config.items()
                if key not in {"source_roles", "source_priority"}
            }
            use_case = retriever_config.get("use_case")
            if use_case in use_cases:
                _apply_legacy_retriever_bindings(
                    use_cases[use_case],
                    retriever_config.get("source_roles") or {},
                    retriever_config.get("source_priority") or [],
                )
            retrievers[retriever_name] = retriever_copy
        stage_copy["use_cases"] = use_cases
        stage_copy["retrievers"] = retrievers
        stages[stage_name] = stage_copy
    return {**raw_rules, "stages": stages}


def _apply_legacy_retriever_bindings(
    selections: list[dict],
    source_roles: dict[str, str],
    source_priority: list[str],
) -> None:
    roles_by_source = {source_id: role for role, source_id in source_roles.items()}
    priority_by_source = {source_id: index + 1 for index, source_id in enumerate(source_priority)}
    for selection in selections:
        source_id = selection.get("source_id")
        if source_id in roles_by_source and not selection.get("role"):
            selection["role"] = roles_by_source[source_id]
        if source_id in priority_by_source and "priority" not in selection:
            selection["priority"] = priority_by_source[source_id]
