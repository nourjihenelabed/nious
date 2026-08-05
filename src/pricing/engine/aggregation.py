"""
Waterfall aggregation (D2, second half).

    "Aggregatable by customer, product, segment, channel, region, period."
                                        -- onboarding plan, deliverable D2

Because the engine keeps one line-grain fact table, aggregation is a groupby and
nothing more. There is no second calculation to keep in sync, which is the whole
reason the fact table exists.

WHAT AGGREGATION MUST NOT DO
----------------------------
1. Not average percentages. Pocket margin % for a segment is
   sum(margin) / sum(pocket), never mean(margin % per deal). Averaging ratios
   weights a EUR 400 order the same as a EUR 400,000 one and is the single most
   common way a pricing dashboard ends up lying.
2. Not mix certified and uncertified rows into one total. Each group reports its
   own coverage, so a segment that is 40% decomposable cannot masquerade as a
   segment that is 100% decomposable.
3. Not drop empty groups silently. A segment with zero certified transactions
   must appear with UNAVAILABLE values, so a reader notices it is missing.
4. Not sum across currencies. The graph carries a currency per region and no FX
   rate anywhere, so adding an AED line to a EUR line produces a number with no
   unit. Every aggregate therefore reports `n_currencies`, and any group
   spanning more than one is refused publication downstream (see
   engine/result.py). The fix at the call site is to include `currency` in the
   grain: `aggregate(w, ["segment", "currency"])`.
"""

from __future__ import annotations

import pandas as pd

from .waterfall import DEDUCTIONS, DIMENSIONS, STEPS, Waterfall

# Friendly aliases so callers can say `aggregate(w, "segment")` or
# `aggregate(w, "region")` without knowing the column names.
DIMENSION_ALIASES: dict[str, str] = {
    "customer": "customer_id",
    "product": "product_id",
    "channel": "channel_id",
    "region": "region_id",
    "transaction": "transaction_id",
    "month": "period",
    "category": "category",
    "family": "product_family",
    "industry": "industry",
    "tier": "tier",
    "size": "size",
    "country": "country",
    "market": "market_type",
    "payment_terms": "payment_terms",
}

# Descriptive labels carried alongside IDs, so an aggregate is readable without
# a second join.
LABEL_FOR: dict[str, str] = {
    "customer_id": "customer_name",
    "product_id": "product_name",
    "region_id": "region_name",
    "channel_id": "channel_type",
}


def resolve(dimension: str | list[str]) -> list[str]:
    """Map user-facing dimension names onto fact-table columns."""
    dims = [dimension] if isinstance(dimension, str) else list(dimension)
    return [DIMENSION_ALIASES.get(d, d) for d in dims]


def aggregate(
    waterfall: Waterfall,
    dimension: str | list[str],
    certified_only: bool = True,
    min_transactions: int = 0,
) -> pd.DataFrame:
    """
    Roll the waterfall up to one or more dimensions.

    Parameters
    ----------
    dimension
        "segment", "customer", ["region", "period"], ... See DIMENSION_ALIASES.
    certified_only
        Sum only fully decomposable transactions (default, and recommended).
        Coverage is reported either way so the exclusion is never invisible.
    min_transactions
        Suppress groups thinner than this. They are not deleted -- they are
        returned with UNAVAILABLE amounts and status "abstained", because a
        disappearing segment is worse than a flagged one.

    Returns one row per group, with every waterfall step, ratios computed from
    sums, and per-group coverage.
    """
    cols = resolve(dimension)
    facts = waterfall.facts
    missing = [c for c in cols if c not in facts.columns]
    if missing:
        raise KeyError(f"Unknown dimension(s) {missing}. Available: {sorted(set(facts.columns) & set(DIMENSIONS) | set(DIMENSION_ALIASES.values()))}")

    scope = facts[facts["certified"]] if certified_only else facts

    # --- sums over the certified scope ------------------------------------
    agg = scope.groupby(cols, dropna=False).agg(
        **{s: (s, lambda x: x.sum(min_count=1)) for s in STEPS},
        n_transactions_certified=("transaction_id", "nunique"),
        n_lines=("transaction_id", "size"),
        quantity=("quantity", "sum"),
    )

    # --- currency safety --------------------------------------------------
    # A group spanning more than one currency has been summed without an FX
    # rate, because the graph does not carry one. Count it here; result.py
    # refuses to publish such a group.
    if "currency" in facts.columns:
        agg["n_currencies"] = scope.groupby(cols, dropna=False)["currency"].nunique()
        agg["currencies"] = scope.groupby(cols, dropna=False)["currency"].apply(
            lambda s: ",".join(sorted(s.dropna().unique()))
        )

    # --- coverage: denominators come from the FULL fact table -------------
    # Using the certified scope for both numerator and denominator would make
    # coverage always 100%, which is exactly the lie we are trying to avoid.
    universe = facts.groupby(cols, dropna=False).agg(
        n_transactions=("transaction_id", "nunique"),
        n_lines_total=("transaction_id", "size"),
    )
    out = universe.join(agg, how="left")
    out["n_transactions_certified"] = out["n_transactions_certified"].fillna(0).astype(int)
    out["coverage_pct"] = (100 * out["n_transactions_certified"] / out["n_transactions"]).round(2)
    if "n_currencies" in out.columns:
        out["n_currencies"] = out["n_currencies"].fillna(0).astype(int)
        out["currencies"] = out["currencies"].fillna("")

    # --- ratios from sums, never means of ratios --------------------------
    out["pocket_margin_pct"] = 100 * out["pocket_margin"] / out["pocket_price"]
    out["price_realisation_pct"] = 100 * out["pocket_price"] / out["list_value"]
    out["on_invoice_discount_pct"] = 100 * out["on_invoice_discount"] / out["gross_value"]
    out["off_invoice_leakage_pct"] = 100 * out["off_invoice_leakage"] / out["gross_value"]
    out["total_leakage_pct"] = 100 * (out["list_value"] - out["pocket_price"]) / out["list_value"]

    # --- thin groups abstain rather than vanish ---------------------------
    out["status"] = "complete"
    out.loc[out["n_transactions_certified"] == 0, "status"] = "unavailable"
    if min_transactions:
        thin = out["n_transactions_certified"] < min_transactions
        out.loc[thin & (out["status"] == "complete"), "status"] = "abstained"
        out.loc[thin, list(STEPS)] = pd.NA

    # --- readable labels ---------------------------------------------------
    for col in cols:
        label = LABEL_FOR.get(col)
        if label and label in facts.columns:
            names = facts.groupby(col, dropna=False)[label].first()
            out.insert(0, label, out.index.get_level_values(col).map(names) if len(cols) > 1 else out.index.map(names))

    return out.reset_index()


def by_all_dimensions(
    waterfall: Waterfall, dimensions: tuple[str, ...] = DIMENSIONS, **kwargs
) -> dict[str, pd.DataFrame]:
    """Every required D2 aggregation in one call. Keys are the dimension names."""
    return {d: aggregate(waterfall, d, **kwargs) for d in dimensions}


def leakage_ranking(
    waterfall: Waterfall, dimension: str = "customer", top: int = 10
) -> pd.DataFrame:
    """
    Where is margin leaking most? The question the module exists to answer.

    Ranked by absolute leaked value, not by percentage: a 40% leak on a small
    account is interesting, a 12% leak on the largest account is a budget.
    """
    agg = aggregate(waterfall, dimension)
    agg = agg[agg["status"] == "complete"].copy()
    agg["leaked_value"] = agg["list_value"] - agg["pocket_price"]
    cols = [c for c in agg.columns if c in
            (*resolve(dimension), *LABEL_FOR.values(), "list_value", "pocket_price",
             "leaked_value", "total_leakage_pct", "off_invoice_leakage",
             "off_invoice_leakage_pct", "pocket_margin_pct", "n_transactions_certified")]
    return agg.sort_values("leaked_value", ascending=False).head(top)[cols].reset_index(drop=True)


def reconciles_with_total(
    waterfall: Waterfall, dimension: str, tolerance: float = 0.01
) -> pd.DataFrame:
    """
    Self-check: any aggregation must sum back to the grand total.

    Cheap, and it catches the whole class of join-fanout bugs that silently
    double-count. Run it after every new dimension is added.
    """
    agg = aggregate(waterfall, dimension)
    rows = []
    for step in STEPS:
        got = agg[step].sum(min_count=1)
        exp = waterfall.totals[step]
        rows.append(
            {
                "step": step,
                "grand_total": exp,
                "sum_of_groups": got,
                "diff": got - exp,
                "ok": bool(abs(got - exp) < tolerance) if pd.notna(exp) and pd.notna(got) else False,
            }
        )
    return pd.DataFrame(rows)


def waterfall_bridge(agg_row: pd.Series) -> pd.DataFrame:
    """
    Turn one aggregated row into a plottable bridge: label, value, and whether
    it is a level or a deduction. Keeps chart code out of notebooks.
    """
    rows = []
    for step in STEPS:
        rows.append(
            {
                "step": step,
                "label": step.replace("_", " ").title(),
                "value": agg_row.get(step),
                "kind": "deduction" if step in DEDUCTIONS else "level",
            }
        )
    return pd.DataFrame(rows)
