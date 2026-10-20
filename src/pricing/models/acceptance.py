"""Acceptance (D6, Week 6): how likely a price is to be agreed, and what that means here."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..engine.waterfall import Waterfall

#: Comparable deals below which a scope cannot support an acceptance curve.
MIN_COMPARABLES = 8
#: Laplace smoothing, so no observed depth is ever reported as certain either way.
SMOOTHING = 1.0

BENCHMARK_INTERPRETATION = (
    "share of comparable completed deals that closed at a discount no deeper than "
    "this one -- a concession benchmark, not an observed win rate"
)
WIN_RATE_INTERPRETATION = "probability this quote is won, from won and lost history"

NO_OUTCOMES = (
    "calibration requires observed outcomes; this source records only completed "
    "transactions, so there is nothing to be right or wrong about"
)


@dataclass(frozen=True)
class Prediction:
    """One acceptance estimate, with everything needed to read it correctly."""

    probability: float
    depth: float
    n_comparables: int
    scope: dict[str, str]
    is_win_rate: bool
    interpretation: str
    status: str = "complete"
    note: str = ""


@dataclass
class AcceptanceModel:
    """Acceptance curves per scope, fitted from whatever the source actually records."""

    mode: str
    scope_columns: tuple[str, ...]
    curves: dict[tuple, np.ndarray]
    reference_price: dict[tuple, float]
    #: The median realised price of each individual product, regardless of the scope the curves
    #: were fitted at.
    product_reference: dict[str, float]
    is_win_rate: bool
    n_scopes: int
    n_abstained: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def interpretation(self) -> str:
        """What the number this model returns actually means."""
        return WIN_RATE_INTERPRETATION if self.is_win_rate else BENCHMARK_INTERPRETATION

    def _key(self, scope: dict[str, str]) -> tuple:
        """The tuple key for a scope dictionary, in fitted column order."""
        return tuple(str(scope[c]) for c in self.scope_columns)

    def probability(self, scope: dict[str, str], depth: float) -> Prediction:
        """Acceptance at a given discount depth, or an abstention that says why."""
        key = self._key(scope)
        depths = self.curves.get(key)
        if depths is None or len(depths) < MIN_COMPARABLES:
            return Prediction(
                probability=float("nan"),
                depth=depth,
                n_comparables=0 if depths is None else len(depths),
                scope=dict(scope),
                is_win_rate=self.is_win_rate,
                interpretation=self.interpretation,
                status="abstained",
                note=(
                    f"fewer than {MIN_COMPARABLES} comparable deals for this scope; "
                    "an acceptance curve over a handful of deals is a shape, not an estimate"
                ),
            )

        n = len(depths)
        # Laplace-smoothed empirical CDF: with k of n deals at or below this depth, the estimate
        # is (k + 1) / (n + 2), which never reaches 0 or 1.
        k = float((depths <= depth).sum())
        p = (k + SMOOTHING) / (n + 2 * SMOOTHING)
        return Prediction(
            probability=float(p),
            depth=float(depth),
            n_comparables=n,
            scope=dict(scope),
            is_win_rate=self.is_win_rate,
            interpretation=self.interpretation,
        )

    def curve(self, scope: dict[str, str], depths: np.ndarray) -> np.ndarray:
        """Acceptance across a grid of depths, for plotting or for the optimiser."""
        return np.array([self.probability(scope, d).probability for d in depths])

    def calibration(self) -> pd.DataFrame:
        """Reliability of the estimates, or a refusal to score them."""
        if not self.is_win_rate:
            return pd.DataFrame(
                [{"bin": "n/a", "predicted": np.nan, "observed": np.nan, "n": 0, "note": NO_OUTCOMES}]
            )
        raise NotImplementedError(
            "logistic mode is not reachable on the shipped profiles; implement "
            "calibration alongside the first source that records lost quotes"
        )

    def summary(self) -> pd.DataFrame:
        """One row per scope: how many comparables and how wide the accepted band."""
        rows = []
        for key, depths in self.curves.items():
            scope = dict(zip(self.scope_columns, key, strict=False))
            rows.append(
                {
                    **scope,
                    "n_comparables": len(depths),
                    "median_depth_pct": round(100 * float(np.median(depths)), 2),
                    "p10_depth_pct": round(100 * float(np.quantile(depths, 0.10)), 2),
                    "p90_depth_pct": round(100 * float(np.quantile(depths, 0.90)), 2),
                    "reference_price": round(self.reference_price.get(key, float("nan")), 2),
                    "usable": len(depths) >= MIN_COMPARABLES,
                }
            )
        return pd.DataFrame(rows).sort_values(
            "n_comparables", ascending=False, ignore_index=True
        )


def discount_depths(w: Waterfall, scope: str | list[str] = "product") -> pd.DataFrame:
    """Realised discount depth per line: how far below list each deal actually closed."""
    profile = w.profile
    facts = w.facts[w.facts["certified"]].copy()
    cols = profile.resolve(scope)
    opening, realised = profile.opening, _deepest_line_level(profile)

    if realised is None or opening not in facts.columns or realised not in facts.columns:
        raise ValueError(
            f"profile {profile.name!r} exposes no list level and no realised line level, "
            "so discount depth cannot be measured"
        )

    usable = facts[(facts[opening] > 0) & facts[realised].notna()].copy()
    usable["depth"] = 1.0 - usable[realised] / usable[opening]
    usable["unit_price"] = usable[realised] / usable[profile.quantity_column].replace(0, np.nan)
    return usable[[*cols, "depth", "unit_price", profile.key]].dropna(subset=["depth"])


def fit(w: Waterfall, scope: str | list[str] = "product") -> AcceptanceModel:
    """Fit acceptance curves, choosing the mode from what the source records."""
    profile = w.profile
    cols = profile.resolve(scope)
    outcome = getattr(profile, "outcome_column", None)

    if outcome and outcome in w.keys.columns:
        raise NotImplementedError(
            "this source records quote outcomes; fit the logistic model rather than "
            "the concession benchmark"
        )

    depths = discount_depths(w, scope)
    curves: dict[tuple, np.ndarray] = {}
    reference: dict[tuple, float] = {}
    for key, group in depths.groupby(cols, dropna=False):
        key_tuple = tuple(str(k) for k in (key if isinstance(key, tuple) else (key,)))
        curves[key_tuple] = np.sort(group["depth"].to_numpy())
        reference[key_tuple] = float(group["unit_price"].median())

    # Always keep a per-product price alongside, whatever scope the curves were fitted at.
    product_reference: dict[str, float] = {}
    if profile.dimension("product") is not None:
        product_col = profile.resolve("product")[0]
        per_product = discount_depths(w, "product")
        product_reference = {
            str(k): float(v)
            for k, v in per_product.groupby(product_col)["unit_price"].median().items()
        }

    n_thin = sum(1 for v in curves.values() if len(v) < MIN_COMPARABLES)
    return AcceptanceModel(
        mode="empirical_acceptance",
        scope_columns=tuple(cols),
        curves=curves,
        reference_price=reference,
        product_reference=product_reference,
        is_win_rate=False,
        n_scopes=len(curves),
        n_abstained=n_thin,
        notes=(
            "no lost quotes exist in this source, so no win rate is estimable",
            BENCHMARK_INTERPRETATION,
        ),
    )


def _deepest_line_level(profile) -> str | None:
    """The lowest level in the chain still knowable per line."""
    from ..analytics.margin import _deepest_line_level as level

    return level(profile)
