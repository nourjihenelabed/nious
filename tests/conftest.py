"""Pin the suite to the shipped configuration, whatever the shell is set to."""

from __future__ import annotations

import os

#: Variables that scope one run and must not leak into the suite.
for name in (
    "PRICING_SOURCE",
    "PRICING_DATA_DIR",
    "PRICING_GRAPH_JSON",
    "PRICING_ELASTICITY_SCOPE",
    "PRICING_ACCEPTANCE_SCOPE",
    "PRICING_SEGMENT_MIN_DEALS",
    "PRICING_SEGMENT_CLUSTERS",
    "PRICING_MIN_WATERFALL",
    "PRICING_MIN_MARGIN_ANALYTICS",
    "PRICING_MIN_SEGMENTATION",
    "PRICING_MIN_ELASTICITY",
    "PRICING_MIN_OPTIMISATION",
):
    os.environ.pop(name, None)

os.environ["PRICING_PROFILE"] = "b2b_industrial"
