"""Retrieval policy and evidence materialization package."""

from src.retrieval.materialization import source_document_from_raw_record
from src.retrieval.policy import DataSourceStrategy, SelectedDataSource, load_data_source_policy, load_retrieval_rules

__all__ = [
    "DataSourceStrategy",
    "SelectedDataSource",
    "load_data_source_policy",
    "load_retrieval_rules",
    "source_document_from_raw_record",
]
