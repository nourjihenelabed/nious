"""Margin analytics (D3): where the margin went, and how unevenly."""

from .margin import (
    leakage_bridge,
    margin_at_risk,
    margin_quadrants,
    price_band,
    run_all,
    summary,
)

__all__ = [
    "leakage_bridge", "price_band", "margin_quadrants", "margin_at_risk",
    "summary", "run_all",
]
