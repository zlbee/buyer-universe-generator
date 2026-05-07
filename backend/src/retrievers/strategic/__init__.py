"""Strategic buyer candidate retrievers for Phase 4 first-pass recall."""

from src.retrievers.strategic.fanout import BuyerCandidateRetriever
from src.retrievers.strategic.acquisition_intent import StrategicAcquisitionIntentRetriever
from src.retrievers.strategic.ma_history import MAHistoryRetriever
from src.retrievers.strategic.sec_transaction_signals import SecTransactionSignalSource
from src.retrievers.strategic.same_sic import SameSicRetriever
from src.retrievers.strategic.shells import (
    AdjacentIndustryRetriever,
    BusinessSimilarityRetriever,
    PeerCompanyRetriever,
    ProductCustomerChannelRetriever,
    SupplyChainRetriever,
)

__all__ = [
    "AdjacentIndustryRetriever",
    "BusinessSimilarityRetriever",
    "BuyerCandidateRetriever",
    "MAHistoryRetriever",
    "PeerCompanyRetriever",
    "ProductCustomerChannelRetriever",
    "SameSicRetriever",
    "SecTransactionSignalSource",
    "StrategicAcquisitionIntentRetriever",
    "SupplyChainRetriever",
]
