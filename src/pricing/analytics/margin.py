"""Margin analytics: where the margin went, and how unevenly."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..engine.aggregation import aggregate
from ..engine.waterfall import Waterfall, ratio
from ..spec import LINE


def default_dimension(profile, preferred: str = "customer") -> str:
    """A dimension this profile actually has, preferring the one asked for."""
    if profile.dimension(preferred):
        return preferred
    for d in profile.dimensions:
        if d.column != profile.key:
            return d.name
    return profile.key


def _reportable(w: Waterfall, dimension: str) -> tuple[bool, str]:
    """Whether every step survives this grain, and why not when it does not."""
    profile = w.profile
    cols = profile.resolve(dimension)
    if profile.key_safe(cols):
        return True, ""
    suppressed = [s.name for s in profile.steps if s.grain != LINE]
    if not suppressed:
        return True, ""
    return False, (
        f"{', '.join(suppressed)} are recorded against the document, not the "
        f"{dimension}; attributing them here would be an allocation the data does "
        "not support"
    )


def leakage_bridge(w: Waterfall, dimension: str | None = None) -> pd.DataFrame:
    """Every deduction between the opening level and pocket price, ranked by size."""
    profile = w.profile
    price = _price_level(profile)
    deductions = [
        s.name for s in profile.steps
        if s.name in profile.deductions
        and profile.step_names.index(s.name) < profile.step_names.index(price)
    ]

    if dimension is None:
        opening = w.totals[profile.opening]
        rows = [
            {
                "concession": step,
                "value": w.totals.get(step),
                "pct_of_opening": (
                    None if pd.isna(opening) or not opening or pd.isna(w.totals.get(step))
                    else round(100 * w.totals[step] / opening, 2)
                ),
            }
            for step in deductions
        ]
        out = pd.DataFrame(rows).sort_values("value", ascending=False, ignore_index=True)
        out["share_of_leakage"] = (100 * out["value"] / out["value"].sum()).round(2)
        return out

    ok, reason = _reportable(w, dimension)
    agg = aggregate(w, dimension)
    keys = profile.resolve(dimension)
    out = agg[[*keys, profile.opening, price, *deductions]].copy()
    out["total_leakage"] = out[profile.opening] - out[price]
    for step in deductions:
        out[f"{step}_share"] = (100 * out[step] / out["total_leakage"]).round(2)
    out["complete"] = ok
    out["note"] = "" if ok else reason
    return out.sort_values("total_leakage", ascending=False, ignore_index=True)


def price_band(w: Waterfall, by: str | None = None, min_documents: int = 3) -> pd.DataFrame:
    """Spread of realised unit price within each group, and what the tail costs."""
    profile = w.profile
    by = by or default_dimension(profile, "product")
    facts = w.facts[w.facts["certified"]]
    cols = profile.resolve(by)
    quantity = profile.quantity_column
    level = _deepest_line_level(profile)

    if not quantity or quantity not in facts.columns or level is None or level not in facts.columns:
        return pd.DataFrame(
            columns=[*cols, "n_documents", "median_price", "p25", "p75", "spread_pct", "note"]
        )

    f = facts[facts[quantity] > 0].copy()
    f["realised_unit_price"] = f[level] / f[quantity]

    g = f.groupby(cols, dropna=False)["realised_unit_price"]
    out = pd.DataFrame(
        {
            "n_documents": f.groupby(cols, dropna=False)[profile.key].nunique(),
            "n_lines": g.size(),
            "median_price": g.median(),
            "p25": g.quantile(0.25),
            "p75": g.quantile(0.75),
            "min_price": g.min(),
            "max_price": g.max(),
        }
    ).reset_index()

    out["spread_pct"] = ratio(out["p75"] - out["p25"], out["median_price"]).round(2)
    out["range_pct"] = ratio(out["max_price"] - out["min_price"], out["median_price"]).round(2)
    out["status"] = np.where(out["n_documents"] >= min_documents, "complete", "abstained")
    out.loc[out["status"] == "abstained", ["spread_pct", "range_pct"]] = np.nan
    out["note"] = np.where(
        out["status"] == "abstained",
        f"fewer than {min_documents} documents -- a spread over one or two deals is not a band",
        "",
    )
    return out.sort_values("spread_pct", ascending=False, ignore_index=True)


def margin_quadrants(
    w: Waterfall, dimension: str | None = None, min_transactions: int = 3
) -> pd.DataFrame:
    """Volume against margin rate, split at the book's own medians."""
    profile = w.profile
    dimension = dimension or default_dimension(profile)
    ok, reason = _reportable(w, dimension)
    agg = aggregate(w, dimension, min_transactions=min_transactions)
    agg = agg[agg["status"] == "complete"].copy()
    if agg.empty or not ok:
        agg["quadrant"] = "unavailable"
        agg["note"] = reason or "no group has enough certified evidence"
        return agg

    price = _price_level(profile)
    volume_median = agg[price].median()
    margin_median = agg["pocket_margin_pct"].median()

    big = agg[price] >= volume_median
    good = agg["pocket_margin_pct"] >= margin_median
    agg["quadrant"] = np.select(
        [big & good, big & ~good, ~big & good],
        ["large & profitable", "large & thin -- negotiate", "small & profitable -- grow"],
        default="small & thin -- serve differently",
    )
    agg["note"] = ""
    keys = profile.resolve(dimension)
    labels = [d.label_column for d in profile.dimensions if d.label_column in agg.columns]
    cols = [
        *dict.fromkeys([*labels, *keys]), price, "pocket_margin_pct",
        "total_leakage_pct", "n_transactions_certified", "quadrant", "note",
    ]
    return agg[[c for c in cols if c in agg.columns]].sort_values(
        price, ascending=False, ignore_index=True
    )


def margin_at_risk(w: Waterfall, floor_pct: float = 10.0) -> pd.DataFrame:
    """Documents earning less than a margin floor, and what closing the gap is worth."""
    profile = w.profile
    price = _price_level(profile)
    keys = w.keys[w.keys["certified"]].copy()
    if profile.closing not in keys.columns or price not in keys.columns:
        return pd.DataFrame(columns=[profile.key, "note"])

    keys["margin_pct"] = ratio(keys[profile.closing], keys[price])
    below = keys[keys["margin_pct"] < floor_pct].copy()
    # Pocket price p must satisfy (p - cost) / p >= floor, i.e. p >= cost / (1 - floor).
    cost = below[price] - below[profile.closing]
    below["required_price"] = cost / (1 - floor_pct / 100.0)
    below["gap_to_floor"] = (below["required_price"] - below[price]).clip(lower=0)
    below["uplift_pct"] = ratio(below["gap_to_floor"], below[price]).round(2)

    dims = [d.column for d in profile.dimensions if d.key_safe and d.column in below.columns]
    cols = [
        profile.key, *dims[:4], price, profile.closing, "margin_pct",
        "required_price", "gap_to_floor", "uplift_pct",
    ]
    return below[[c for c in dict.fromkeys(cols) if c in below.columns]].sort_values(
        "gap_to_floor", ascending=False, ignore_index=True
    )


def summary(w: Waterfall, floor_pct: float = 10.0) -> pd.DataFrame:
    """The four headline margin numbers, with their denominators stated."""
    profile = w.profile
    price = _price_level(profile)
    at_risk = margin_at_risk(w, floor_pct)
    certified = w.keys[w.keys["certified"]]
    n = len(certified)

    return pd.DataFrame(
        [
            {
                "metric": f"{w.profile.key.removesuffix('_id')}s earning under {floor_pct:.0f}% margin",
                "value": len(at_risk),
                "of": n,
                "pct": round(100 * len(at_risk) / n, 2) if n else np.nan,
            },
            {
                "metric": "money kept (pocket price) on those",
                "value": round(float(at_risk[price].sum()), 2) if len(at_risk) else 0.0,
                "of": round(float(certified[price].sum()), 2) if n else np.nan,
                "pct": round(100 * at_risk[price].sum() / certified[price].sum(), 2)
                if n and certified[price].sum()
                else np.nan,
            },
            {
                "metric": f"extra money needed to bring them up to {floor_pct:.0f}%",
                "value": round(float(at_risk["gap_to_floor"].sum()), 2) if len(at_risk) else 0.0,
                "of": round(float(w.totals.get(profile.closing, np.nan)), 2),
                "pct": np.nan,
            },
            {
                "metric": "money lost between list price and pocket price",
                "value": round(float(w.totals[profile.opening] - w.totals[price]), 2)
                if pd.notna(w.totals[profile.opening])
                else np.nan,
                "of": round(float(w.totals[profile.opening]), 2),
                "pct": round(
                    100 * (w.totals[profile.opening] - w.totals[price]) / w.totals[profile.opening], 2
                )
                if pd.notna(w.totals[profile.opening]) and w.totals[profile.opening]
                else np.nan,
            },
        ]
    )


def _deepest_line_level(profile) -> str | None:
    """The lowest level in the chain still knowable per line."""
    line_levels = [
        s.name for s in profile.steps
        if s.name in profile.levels and s.grain == LINE
    ]
    return line_levels[-1] if line_levels else None


def _price_level(profile) -> str:
    """The level margin is measured against."""
    from ..engine.waterfall import _price_level as level

    return level(profile)


def run_all(w: Waterfall, floor_pct: float = 10.0) -> dict[str, pd.DataFrame]:
    """Every margin view in one call; keys become report section names."""
    grouping = default_dimension(w.profile)
    return {
        "summary": summary(w, floor_pct),
        "leakage_bridge": leakage_bridge(w),
        f"leakage_by_{grouping}": leakage_bridge(w, grouping),
        "price_band": price_band(w),
        "margin_quadrants": margin_quadrants(w),
        "margin_at_risk": margin_at_risk(w, floor_pct),
    }
