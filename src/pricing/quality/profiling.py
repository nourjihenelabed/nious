"""
Graph / snapshot profiling.

Week 1 asks for "counts, distributions, null rates". This module produces those
as data (DataFrames), not as printed text, so the notebook, the CLI script and
later the Pricing Data Readiness Score (D3, Week 4) all read the same numbers.

Nothing here judges the data. It reports. Interpretation lives in
docs/WEEK1_DATA_WRITEUP.md.
"""

from __future__ import annotations

import pandas as pd

from ..graph.source import GraphSource

# Discount taxonomy -- imported here rather than hardcoded in three places.
from ..config import OFF_INVOICE, ON_INVOICE


# --------------------------------------------------------------------------
# 1. Shape
# --------------------------------------------------------------------------
def table_counts(src: GraphSource) -> pd.DataFrame:
    """Row and column count per table -- the graph's basic dimensions."""
    rows = []
    for name, df in src.tables().items():
        rows.append({"table": name, "rows": len(df), "columns": df.shape[1]})
    return pd.DataFrame(rows).sort_values("rows", ascending=False, ignore_index=True)


# --------------------------------------------------------------------------
# 2. Null rates
# --------------------------------------------------------------------------
def null_rates(src: GraphSource) -> pd.DataFrame:
    """
    Per-column null count and rate across every table.

    This is the single most important Week 1 artefact: every null is a decision
    the waterfall will have to make later (flag as 'unavailable' vs. impute vs.
    exclude), and the acceptance criteria forbid silently zeroing them.
    """
    rows = []
    for name, df in src.tables().items():
        for col in df.columns:
            n_null = int(df[col].isna().sum())
            rows.append(
                {
                    "table": name,
                    "column": col,
                    "dtype": str(df[col].dtype),
                    "n_rows": len(df),
                    "n_null": n_null,
                    "null_rate_pct": round(100 * n_null / len(df), 2) if len(df) else 0.0,
                    "n_unique": int(df[col].nunique(dropna=True)),
                }
            )
    return pd.DataFrame(rows)


def null_summary(src: GraphSource) -> pd.DataFrame:
    """Only the columns that actually have nulls -- the shortlist to ask about."""
    nr = null_rates(src)
    return nr[nr["n_null"] > 0].sort_values(
        "null_rate_pct", ascending=False, ignore_index=True
    )


# --------------------------------------------------------------------------
# 3. Distributions
# --------------------------------------------------------------------------
def numeric_profile(src: GraphSource) -> pd.DataFrame:
    """Descriptive stats for every numeric column, plus a negative-value check."""
    rows = []
    for name, df in src.tables().items():
        for col in df.select_dtypes("number").columns:
            s = df[col].dropna()
            if s.empty:
                continue
            rows.append(
                {
                    "table": name,
                    "column": col,
                    "n": len(s),
                    "mean": s.mean(),
                    "std": s.std(),
                    "min": s.min(),
                    "p25": s.quantile(0.25),
                    "median": s.median(),
                    "p75": s.quantile(0.75),
                    "max": s.max(),
                    # Semantic type check (Brief 5.3): monetary values must be
                    # positive, quantities non-negative.
                    "n_negative": int((s < 0).sum()),
                    "n_zero": int((s == 0).sum()),
                }
            )
    return pd.DataFrame(rows)


def categorical_profile(src: GraphSource, max_levels: int = 12) -> pd.DataFrame:
    """Value counts for low-cardinality text columns (segment, tier, region...)."""
    rows = []
    for name, df in src.tables().items():
        for col in df.select_dtypes("object").columns:
            vc = df[col].value_counts(dropna=False)
            if len(vc) > max_levels:  # skip IDs and free text
                continue
            for value, n in vc.items():
                rows.append(
                    {
                        "table": name,
                        "column": col,
                        "value": "<null>" if pd.isna(value) else value,
                        "n": int(n),
                        "pct": round(100 * n / len(df), 2),
                    }
                )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 4. Referential integrity
# --------------------------------------------------------------------------
def referential_integrity(src: GraphSource) -> pd.DataFrame:
    """
    Do the foreign keys resolve? In graph terms: are there dangling edges?

    An orphan transaction cannot be attributed to a customer or segment, so it
    would silently distort every aggregate. We must know the count before we
    build anything on top.
    """
    txn = src.transactions()
    lines = src.transaction_lines()
    tdisc = src.transaction_discounts()

    checks = [
        ("transactions.customer_id", txn["customer_id"], set(src.customers()["customer_id"])),
        ("transactions.channel_id", txn["channel_id"], set(src.channels()["channel_id"])),
        ("customers.region_id", src.customers()["region_id"], set(src.regions()["region_id"])),
        ("transaction_lines.transaction_id", lines["transaction_id"], set(txn["transaction_id"])),
        ("transaction_lines.product_id", lines["product_id"], set(src.products()["product_id"])),
        ("transaction_discounts.transaction_id", tdisc["transaction_id"], set(txn["transaction_id"])),
        ("transaction_discounts.discount_id", tdisc["discount_id"], set(src.discounts()["discount_id"])),
    ]

    rows = []
    for label, series, valid in checks:
        missing = series[~series.isin(valid) & series.notna()]
        rows.append(
            {
                "check": label,
                "n_values": int(series.notna().sum()),
                "n_unresolved": int(len(missing)),
                "examples": ", ".join(map(str, missing.unique()[:3])) or "-",
                "ok": len(missing) == 0,
            }
        )

    # Transactions with no line items at all -- a waterfall cannot start without
    # a list-price basis, so these must be excluded, not zeroed.
    no_lines = set(txn["transaction_id"]) - set(lines["transaction_id"])
    rows.append(
        {
            "check": "transactions with >=1 CONTAINS line",
            "n_values": len(txn),
            "n_unresolved": len(no_lines),
            "examples": ", ".join(sorted(no_lines)[:3]) or "-",
            "ok": not no_lines,
        }
    )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 5. Pricing-specific reconciliation
# --------------------------------------------------------------------------
def discount_coverage(src: GraphSource) -> pd.DataFrame:
    """How often each discount type is applied, and how much it is worth."""
    tdisc = src.transaction_discounts()
    disc = src.discounts()
    n_txn = src.transactions()["transaction_id"].nunique()

    m = tdisc.merge(disc[["discount_id", "type", "category", "rate"]], on="discount_id", how="left")
    g = (
        m.groupby(["discount_id", "type", "category", "rate"], dropna=False)
        .agg(
            n_applications=("transaction_id", "size"),
            n_transactions=("transaction_id", "nunique"),
            n_amount_missing=("discount_amount_applied", lambda s: int(s.isna().sum())),
            total_amount=("discount_amount_applied", "sum"),
            mean_amount=("discount_amount_applied", "mean"),
        )
        .reset_index()
    )
    g["pct_of_transactions"] = (100 * g["n_transactions"] / n_txn).round(1)
    return g.sort_values("total_amount", ascending=False, ignore_index=True)


def amount_reconciliation(src: GraphSource) -> pd.DataFrame:
    """
    Test the arithmetic the Data Contract implies, so we know what
    gross_amount / net_amount actually mean in this snapshot.

    Three hypotheses are checked per transaction:
      H1  gross_amount == sum of CONTAINS.line_total
      H2  net_amount   == gross - on-invoice discounts   (the textbook reading:
          net/invoice price is what appears on the invoice)
      H3  net_amount   == gross - ALL discounts

    Whichever holds tells us where `net_amount` sits on the waterfall. If none
    holds, we do not guess -- we ask, and meanwhile compute the waterfall from
    the discount edges rather than from net_amount.
    """
    txn = src.transactions().set_index("transaction_id")
    lines = src.transaction_lines()
    tdisc = src.transaction_discounts()
    disc = src.discounts()[["discount_id", "type"]]

    line_sum = lines.groupby("transaction_id")["line_total"].sum().reindex(txn.index)
    m = tdisc.merge(disc, on="discount_id", how="left")
    on = (
        m[m["type"] == ON_INVOICE]
        .groupby("transaction_id")["discount_amount_applied"].sum()
        .reindex(txn.index).fillna(0)
    )
    off = (
        m[m["type"] == OFF_INVOICE]
        .groupby("transaction_id")["discount_amount_applied"].sum()
        .reindex(txn.index).fillna(0)
    )

    def _stat(label: str, diff: pd.Series) -> dict:
        d = diff.dropna()
        return {
            "hypothesis": label,
            "n_tested": len(d),
            "max_abs_diff": d.abs().max(),
            "mean_abs_diff": d.abs().mean(),
            # 0.01% of a typical transaction, per the D2 acceptance criterion
            "n_within_0.01pct": int((d.abs() <= 0.0001 * txn["gross_amount"].loc[d.index].abs()).sum()),
            "holds": bool(d.abs().max() < 0.01),
        }

    return pd.DataFrame(
        [
            _stat("H1: gross == sum(line_total)", txn["gross_amount"] - line_sum),
            _stat("H2: net == gross - on-invoice", txn["gross_amount"] - on - txn["net_amount"]),
            _stat("H3: net == gross - all discounts", txn["gross_amount"] - on - off - txn["net_amount"]),
            _stat("H4: net == gross - off-invoice", txn["gross_amount"] - off - txn["net_amount"]),
        ]
    )


def list_price_coverage(src: GraphSource) -> pd.DataFrame:
    """
    Can we build a list-price basis? The waterfall starts at list price, which
    lives on Product, not on the transaction. Any line whose product has no
    list_price or unit_cost is a line the waterfall cannot fully decompose.
    """
    lines = src.transaction_lines()
    prod = src.products()[["product_id", "list_price", "unit_cost"]]
    m = lines.merge(prod, on="product_id", how="left")

    n = len(m)
    return pd.DataFrame(
        [
            {"metric": "transaction lines", "n": n, "pct": 100.0},
            {
                "metric": "lines with a product list_price",
                "n": int(m["list_price"].notna().sum()),
                "pct": round(100 * m["list_price"].notna().mean(), 2),
            },
            {
                "metric": "lines with a product unit_cost",
                "n": int(m["unit_cost"].notna().sum()),
                "pct": round(100 * m["unit_cost"].notna().mean(), 2),
            },
            {
                "metric": "lines fully decomposable (list + cost)",
                "n": int((m["list_price"].notna() & m["unit_cost"].notna()).sum()),
                "pct": round(100 * (m["list_price"].notna() & m["unit_cost"].notna()).mean(), 2),
            },
            {
                "metric": "lines priced ABOVE list (unit_price > list_price)",
                "n": int((m["unit_price"] > m["list_price"]).sum()),
                "pct": round(100 * (m["unit_price"] > m["list_price"]).mean(), 2),
            },
        ]
    )


def date_profile(src: GraphSource) -> pd.DataFrame:
    """Transaction volume by month -- checks coverage and spots future dates."""
    txn = src.transactions()
    d = txn.dropna(subset=["date"]).copy()
    d["month"] = d["date"].dt.to_period("M").astype(str)
    return (
        d.groupby("month")
        .agg(n_transactions=("transaction_id", "size"), gross_amount=("gross_amount", "sum"))
        .reset_index()
    )


def run_all(src: GraphSource) -> dict[str, pd.DataFrame]:
    """Every profile in one call. Keys become report section names."""
    return {
        "table_counts": table_counts(src),
        "null_summary": null_summary(src),
        "null_rates": null_rates(src),
        "numeric_profile": numeric_profile(src),
        "categorical_profile": categorical_profile(src),
        "referential_integrity": referential_integrity(src),
        "discount_coverage": discount_coverage(src),
        "amount_reconciliation": amount_reconciliation(src),
        "list_price_coverage": list_price_coverage(src),
        "date_profile": date_profile(src),
    }
