"""Waterfall aggregation (D2): roll the waterfall up to any dimension it supports."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..spec import KEY, LINE
from .waterfall import GRAIN_NOTE, Waterfall, complete_sum, ratio


def resolve(dimension: str | list[str], w: Waterfall | None = None) -> list[str]:
    """Map user-facing dimension names onto fact-table columns."""
    from ..profiles import get_profile

    profile = w.profile if w is not None else get_profile()
    return profile.resolve(dimension)


def _label_columns(w: Waterfall) -> dict[str, str]:
    """Dimension column -> the human-readable column that names it."""
    return {d.column: d.label_column for d in w.profile.dimensions if d.label_column}


def aggregate(
    waterfall: Waterfall,
    dimension: str | list[str],
    certified_only: bool = True,
    min_transactions: int = 0,
) -> pd.DataFrame:
    """Roll the waterfall up to one or more dimensions, with per-group coverage."""
    profile = waterfall.profile
    cols = profile.resolve(dimension)
    key = profile.key

    key_safe = profile.key_safe(cols)
    frame = waterfall.keys if key_safe else waterfall.facts

    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise KeyError(
            f"Unknown dimension(s) {missing}. Available: "
            f"{sorted(set(frame.columns) & set(profile.dimension_columns))}"
        )

    reportable = [
        s.name for s in profile.steps
        if (key_safe or s.grain == LINE) and s.name in frame.columns
    ]
    suppressed = [s for s in profile.step_names if s not in reportable]

    scope = frame[frame["certified"]] if certified_only else frame

    agg_spec: dict[str, tuple] = {s: (s, complete_sum) for s in reportable}
    agg_spec["n_transactions_certified"] = (key, "nunique")
    agg_spec["n_lines"] = (key, "size")
    if profile.quantity_column and profile.quantity_column in frame.columns:
        agg_spec["quantity"] = (profile.quantity_column, "sum")

    agg = scope.groupby(cols, dropna=False).agg(**agg_spec)

    currency = profile.currency_column
    if currency and currency in frame.columns:
        grouped = scope.groupby(cols, dropna=False)[currency]
        agg["n_currencies"] = grouped.nunique()
        agg["currencies"] = grouped.apply(lambda s: ",".join(sorted(s.dropna().unique())))

    universe = frame.groupby(cols, dropna=False).agg(
        n_transactions=(key, "nunique"),
        n_lines_total=(key, "size"),
    )
    out = universe.join(agg, how="left")
    out["n_transactions_certified"] = out["n_transactions_certified"].fillna(0).astype(int)
    out["coverage_pct"] = (100 * out["n_transactions_certified"] / out["n_transactions"]).round(2)
    if "n_currencies" in out.columns:
        out["n_currencies"] = out["n_currencies"].fillna(0).astype(int)
        out["currencies"] = out["currencies"].fillna("")

    for step in suppressed:
        out[step] = np.nan
    out["unavailable_steps"] = ";".join(suppressed)
    out["grain_note"] = (
        ""
        if not suppressed
        else GRAIN_NOTE.format(
            step=", ".join(suppressed),
            grain=f"{profile.entity.split()[0]} document",
            by="+".join(cols),
        )
    )

    out = out[[*universe.columns, *profile.step_names, *(
        c for c in out.columns if c not in {*universe.columns, *profile.step_names}
    )]]

    price = _price_level(profile)
    out["pocket_margin_pct"] = ratio(out[profile.closing], out[price])
    out["price_realisation_pct"] = ratio(out[price], out[profile.opening])
    for name, num, den in _rate_definitions(profile):
        if num in out.columns and den in out.columns:
            out[name] = ratio(out[num], out[den])
    out["total_leakage_pct"] = ratio(out[profile.opening] - out[price], out[profile.opening])

    out["status"] = "complete"
    out.loc[out["n_transactions_certified"] == 0, "status"] = "unavailable"
    if min_transactions:
        thin = out["n_transactions_certified"] < min_transactions
        out.loc[thin & (out["status"] == "complete"), "status"] = "abstained"
        out.loc[thin, list(profile.step_names)] = pd.NA

    labels = _label_columns(waterfall)
    source = waterfall.facts
    for col in cols:
        label = labels.get(col)
        if label and label in source.columns:
            names = source.groupby(col, dropna=False)[label].first()
            out.insert(
                0,
                label,
                out.index.get_level_values(col).map(names) if len(cols) > 1 else out.index.map(names),
            )

    return out.reset_index()


def _price_level(profile) -> str:
    """The level margin is measured against."""
    from .waterfall import _price_level as level

    return level(profile)


def _rate_definitions(profile) -> list[tuple[str, str, str]]:
    """Percentage columns for each deduction, against the level above it."""
    out = []
    for i, s in enumerate(profile.steps):
        if s.name in profile.deductions and i > 0:
            out.append((f"{s.name}_pct", s.name, profile.steps[i - 1].name))
    return out


def by_all_dimensions(
    waterfall: Waterfall, dimensions: tuple[str, ...] | None = None, **kwargs
) -> dict[str, pd.DataFrame]:
    """Every required D2 aggregation in one call, keyed by dimension name."""
    from .waterfall import DIMENSIONS

    dims = dimensions or DIMENSIONS
    return {d: aggregate(waterfall, d, **kwargs) for d in dims}


def leakage_ranking(
    waterfall: Waterfall, dimension: str = "customer", top: int = 10
) -> pd.DataFrame:
    """Where margin leaks most, ranked by absolute leaked value rather than by rate."""
    profile = waterfall.profile
    agg = aggregate(waterfall, dimension)
    agg = agg[agg["status"] == "complete"].copy()
    price = _price_level(profile)
    agg["leaked_value"] = agg[profile.opening] - agg[price]

    labels = set(_label_columns(waterfall).values())
    keep = {
        *profile.resolve(dimension), *labels, profile.opening, price, "leaked_value",
        "total_leakage_pct", "pocket_margin_pct", "n_transactions_certified",
        *(f"{d}_pct" for d in profile.deductions), *profile.deductions,
    }
    cols = [c for c in agg.columns if c in keep]
    return agg.sort_values("leaked_value", ascending=False).head(top)[cols].reset_index(drop=True)


def reconciles_with_total(
    waterfall: Waterfall, dimension: str, tolerance: float = 0.01
) -> pd.DataFrame:
    """Self-check: an aggregation must sum back to the grand total, step by step."""
    profile = waterfall.profile
    agg = aggregate(waterfall, dimension)
    key_safe = profile.key_safe(profile.resolve(dimension))

    rows = []
    for step in profile.step_names:
        grain = profile.step(step).grain
        can_report = key_safe or grain == LINE
        got = agg[step].sum(min_count=1)
        exp = waterfall.totals[step]
        if can_report:
            ok = bool(abs(got - exp) < tolerance) if pd.notna(exp) and pd.notna(got) else False
        else:
            ok = bool(agg[step].isna().all())
        rows.append(
            {
                "step": step,
                "reportable_at_this_grain": can_report,
                "grand_total": exp,
                "sum_of_groups": got,
                "diff": got - exp,
                "ok": ok,
            }
        )
    return pd.DataFrame(rows)


def waterfall_bridge(agg_row: pd.Series, waterfall: Waterfall | None = None) -> pd.DataFrame:
    """Turn one aggregated row into a plottable bridge of levels and deductions."""
    from ..profiles import get_profile

    profile = waterfall.profile if waterfall is not None else get_profile()
    return pd.DataFrame(
        [
            {
                "step": s.name,
                "label": s.display(),
                "value": agg_row.get(s.name),
                "kind": s.kind.value,
                "grain": s.grain,
            }
            for s in profile.steps
        ]
    )


#: Kept so callers that imported the old lookup keep working. The mapping now
#: lives on the profile, where a client can change it without editing the engine.
DIMENSION_ALIASES: dict[str, str] = {}
LABEL_FOR: dict[str, str] = {}


def _refresh_legacy_lookups() -> None:
    """Populate the back-compat lookups from the default profile."""
    from ..profiles import get_profile

    profile = get_profile()
    DIMENSION_ALIASES.update({d.name: d.column for d in profile.dimensions})
    LABEL_FOR.update({d.column: d.label_column for d in profile.dimensions if d.label_column})


_refresh_legacy_lookups()

__all__ = [
    "aggregate", "by_all_dimensions", "leakage_ranking", "reconciles_with_total",
    "waterfall_bridge", "resolve", "complete_sum", "KEY", "LINE",
]
