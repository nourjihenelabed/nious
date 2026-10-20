"""The engine -> narrative result contract (D8). Track B's only import."""

from .schema import (
    CONTRACT_VERSION,
    Confidence,
    ConfidenceBand,
    ElasticityEstimate,
    EngineResult,
    Evidence,
    Money,
    PriceRange,
    PriceRecommendation,
    Provenance,
    ResultStatus,
    SegmentAssignment,
    WaterfallRecord,
    validate,
)

__all__ = [
    "CONTRACT_VERSION", "EngineResult", "Provenance",
    "WaterfallRecord", "SegmentAssignment", "PriceRecommendation", "ElasticityEstimate",
    "Money", "Evidence", "Confidence", "ConfidenceBand", "ResultStatus", "PriceRange",
    "validate",
]
