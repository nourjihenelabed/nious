"""
The engine -> narrative result contract (D8).

Track B imports from here and nowhere else in this codebase. Keeping the
boundary in one package is what lets us freeze it in Week 7 without freezing
the engine internals.
"""

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
