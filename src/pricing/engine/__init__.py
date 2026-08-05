"""
The pricing engine.

    waterfall.py  D2 price waterfall -- one line-grain fact table
    aggregate.py  roll it up to any dimension
    result.py     translate internals into the Track B contract
    pipeline.py   validate -> compute -> aggregate -> contract -> track

Later phases add: segmentation.py (D4), elasticity.py (D5),
win_probability.py + optimiser.py (D6).
"""

from .aggregation import aggregate, by_all_dimensions, leakage_ranking, reconciles_with_total
from .pipeline import PipelineRun, run
from .waterfall import STEPS, Waterfall, compute, for_customer

__all__ = [
    "run", "PipelineRun",
    "compute", "for_customer", "Waterfall", "STEPS",
    "aggregate", "by_all_dimensions", "leakage_ranking", "reconciles_with_total",
]
