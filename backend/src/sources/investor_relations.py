"""Backward-compatible imports for investor-relations discovery.

The implementation lives in :mod:`src.pipelines.investor_relations` because it
is a discovery workflow, not a provider source adapter primitive.
"""

from src.pipelines.investor_relations import (
    IRPageCandidateOutput,
    IRPageDiscoveryOutput,
    IRPageDiscoveryResult,
    InvestorRelationsPageDiscovery,
    ir_discovery_json_schema,
    ir_discovery_system_prompt,
)

__all__ = [
    "IRPageCandidateOutput",
    "IRPageDiscoveryOutput",
    "IRPageDiscoveryResult",
    "InvestorRelationsPageDiscovery",
    "ir_discovery_json_schema",
    "ir_discovery_system_prompt",
]
