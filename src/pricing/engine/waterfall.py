"""
Price Waterfall Engine (D2).

The deterministic core of the module: list price -> pocket margin, with every
deduction visible. No modelling, no estimation. If a number here is wrong, it is
wrong for a reason we can point at.

    List value            = sum over lines of  quantity x Product.list_price
    (-) negotiated off-list          the gap between list and what was invoiced
    = Gross (invoiced) value = sum over lines of  CONTAINS.line_total
    (-) on-invoice discounts         DISCOUNTED_BY where Discount.type = on-invoice
    = Invoice price
    (-) off-invoice leakage          rebates, freight, terms, co-op, side letters
    = Pocket price
    (-) COGS               = sum over lines of  quantity x Product.unit_cost
    = Pocket margin

DESIGN: ONE FACT TABLE, AT LINE GRAIN
-------------------------------------
Everything is computed once at the finest grain available -- the transaction
LINE -- and every aggregate (customer, product, segment, channel, region,
period) is a groupby over that one table. The alternative, a separate
calculation per grain, is how waterfalls end up disagreeing with themselves.

Discounts, however, hang off the *transaction*, not the line. To reach line
grain we allocate each transaction's discount across its lines pro-rata by
line_total:

    line_share = line_total / sum(line_total for that transaction)

This is an ALLOCATION ASSUMPTION and it is declared, not hidden:

  * It is exact in aggregate -- allocated amounts sum back to the transaction
    total to the cent, so transaction-grain rollups are unaffected. Verified in
    tests/test_engine.py::test_line_rollup_equals_transaction.
  * It is an assumption at PRODUCT grain. A rebate triggered by one product but
    spread across the whole order will be mis-attributed. Pro-rata by value is
    the standard convention and the only defensible default when the graph does
    not tell us which line a discount applies to.
  * If AXIOM later exposes a line-level DISCOUNTED_BY edge, drop the allocation
    and use it. `ALLOCATION_METHOD` marks the one place to change.

THE RULE
--------
Missing is not zero, and a partial sum is not a result. A step whose inputs are
incomplete is UNAVAILABLE (NaN) and the row is not certified. Headline totals
cover certified rows only, with coverage always reported alongside.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import OFF_INVOICE, ON_INVOICE
from ..graph.source import GraphSource

ALLOCATION_METHOD = "pro_rata_by_line_total"

# The waterfall steps, in order. Single source of truth: reports, plots, the
# result contract and the reconciliation tests all read this.
STEPS: tuple[str, ...] = (
    "list_value",
    "negotiated_off_list",
    "gross_value",
    "on_invoice_discount",
    "invoice_price",
    "off_invoice_leakage",
    "pocket_price",
    "cogs",
    "pocket_margin",
)

# Steps that are subtractions rather than levels -- used for plotting and for
# checking that levels reconcile.
DEDUCTIONS: tuple[str, ...] = (
    "negotiated_off_list",
    "on_invoice_discount",
    "off_invoice_leakage",
    "cogs",
)

# The dimensions D2 requires the waterfall to aggregate by.
DIMENSIONS: tuple[str, ...] = (
    "customer_id",
    "product_id",
    "segment",
    "channel_id",
    "region_id",
    "period",
)


# ==========================================================================
@dataclass
class Waterfall:
    """
    The result of one waterfall computation.

    `facts` is the line-grain table -- the thing everything else is derived
    from. `totals` and `flags` are conveniences over it.
    """

    facts: pd.DataFrame
    totals: pd.Series
    flags: pd.DataFrame
    snapshot: dict = field(default_factory=dict)
    allocation_method: str = ALLOCATION_METHOD

    def by_transaction(self) -> pd.DataFrame:
        from .aggregation import aggregate  # local import avoids a circular import

        return aggregate(self, "transaction_id")

    def summary(self) -> str:
        return format_waterfall(self.totals)


# ==========================================================================
# Fact table construction
# ==========================================================================
def _dimension_columns(src: GraphSource) -> pd.DataFrame:
    """Everything needed to slice the waterfall, joined once, up front."""
    txn = src.transactions()[
        ["transaction_id", "date", "customer_id", "channel_id", "payment_terms",
         "gross_amount", "net_amount"]
    ]
    cust = src.customers()[
        ["customer_id", "name", "segment", "region_id", "tier", "industry", "size"]
    ].rename(columns={"name": "customer_name"})
    reg = src.regions()[
        ["region_id", "name", "currency", "market_type", "country"]
    ].rename(columns={"name": "region_name"})
    chan = src.channels()[
        ["channel_id", "type", "cost_to_serve", "commission_rate"]
    ].rename(columns={"type": "channel_type"})

    return (
        txn.merge(cust, on="customer_id", how="left")
        .merge(reg, on="region_id", how="left")
        .merge(chan, on="channel_id", how="left")
    )


def _allocated_discounts(src: GraphSource, lines: pd.DataFrame) -> pd.DataFrame:
    """
    Transaction-level discounts, allocated to lines pro-rata by line_total.

    Returns one row per (transaction_id, product_id) with allocated on-invoice
    and off-invoice amounts, plus counts of edges whose amount was unknown
    (those block certification for the whole transaction).
    """
    tdisc = src.transaction_discounts().merge(
        src.discounts()[["discount_id", "type"]], on="discount_id", how="left"
    )

    # Every transaction in scope, including those with no discount edges at all.
    # Reindexing onto this is what stops an undiscounted full-price sale from
    # falling through the pivot and being read as "amount unknown".
    all_ids = pd.Index(lines["transaction_id"].unique(), name="transaction_id")

    # Per-transaction totals by discount type. min_count=1 so an all-NaN group
    # stays NaN rather than silently becoming 0.
    per_txn = (
        tdisc.pivot_table(
            index="transaction_id",
            columns="type",
            values="discount_amount_applied",
            aggfunc=lambda s: s.sum(min_count=1),
        )
        .reindex(index=all_ids, columns=[ON_INVOICE, OFF_INVOICE])
        .rename(columns={ON_INVOICE: "on_invoice_discount", OFF_INVOICE: "off_invoice_leakage"})
    )

    # "We gave something away but do not know how much" is a certification
    # blocker, not a zero.
    unknown = (
        tdisc.assign(_bad=tdisc["discount_amount_applied"].isna())
        .pivot_table(index="transaction_id", columns="type", values="_bad", aggfunc="sum")
        .reindex(index=all_ids, columns=[ON_INVOICE, OFF_INVOICE])
        .fillna(0)
        .rename(
            columns={
                ON_INVOICE: "n_unknown_on_invoice_edges",
                OFF_INVOICE: "n_unknown_off_invoice_edges",
            }
        )
    )

    # A transaction with NO edge of a given type genuinely had no such discount
    # -> a true zero. That is different from "edges exist, amounts unknown", and
    # different again from "we never looked". This is the line that keeps an
    # undiscounted sale out of the uncertified pile.
    has_edge = (
        tdisc.pivot_table(
            index="transaction_id", columns="type", values="discount_id", aggfunc="size"
        )
        .reindex(index=all_ids, columns=[ON_INVOICE, OFF_INVOICE])
        .fillna(0)
    )
    for kind, col in ((ON_INVOICE, "on_invoice_discount"), (OFF_INVOICE, "off_invoice_leakage")):
        per_txn.loc[has_edge[kind] == 0, col] = 0.0

    # --- allocate to lines ------------------------------------------------
    denom = lines.groupby("transaction_id")["line_total"].transform("sum")
    # Guard: a zero-value order cannot be allocated pro-rata by value, so fall
    # back to an equal split. The amounts still reconcile exactly.
    n_lines = lines.groupby("transaction_id")["line_total"].transform("size")
    share = np.where(denom != 0, lines["line_total"] / denom, 1.0 / n_lines)

    alloc = lines[["transaction_id", "product_id"]].copy()
    alloc["_share"] = share
    alloc = alloc.merge(
        per_txn.reindex(columns=["on_invoice_discount", "off_invoice_leakage"]),
        left_on="transaction_id",
        right_index=True,
        how="left",
    )
    for col in ("on_invoice_discount", "off_invoice_leakage"):
        alloc[col] = alloc[col] * alloc["_share"]

    alloc = alloc.merge(unknown, left_on="transaction_id", right_index=True, how="left")
    cols = ["n_unknown_on_invoice_edges", "n_unknown_off_invoice_edges"]
    alloc[cols] = alloc[cols].fillna(0)
    return alloc.drop(columns="_share")


def build_facts(src: GraphSource, transactions: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    The line-grain waterfall fact table -- the single source of truth.

    One row per (transaction, product) with every waterfall step, every
    dimension needed to slice it, and the flags that decide certification.
    """
    txn_dims = _dimension_columns(src)
    if transactions is not None:
        txn_dims = txn_dims[txn_dims["transaction_id"].isin(transactions["transaction_id"])]

    lines = src.transaction_lines()
    lines = lines[lines["transaction_id"].isin(txn_dims["transaction_id"])].copy()

    prods = src.products()[
        ["product_id", "name", "category", "sub_category", "list_price", "unit_cost", "product_family"]
    ].rename(columns={"name": "product_name"})

    f = lines.merge(prods, on="product_id", how="left").merge(txn_dims, on="transaction_id", how="left")

    # --- levels and deductions -------------------------------------------
    f["list_value"] = f["quantity"] * f["list_price"]
    f["cogs"] = f["quantity"] * f["unit_cost"]
    f["gross_value"] = f["line_total"]

    f = f.merge(_allocated_discounts(src, lines), on=["transaction_id", "product_id"], how="left")

    f["negotiated_off_list"] = f["list_value"] - f["gross_value"]
    f["invoice_price"] = f["gross_value"] - f["on_invoice_discount"]
    f["pocket_price"] = f["invoice_price"] - f["off_invoice_leakage"]
    f["pocket_margin"] = f["pocket_price"] - f["cogs"]

    # --- certification ----------------------------------------------------
    # A line is certified when every step is known. But a transaction is only
    # certified if ALL of its lines are: a partially-known order produces a
    # partial sum, and a partial sum is not a result.
    f["line_certified"] = (
        f["list_value"].notna()
        & f["cogs"].notna()
        & f["on_invoice_discount"].notna()
        & f["off_invoice_leakage"].notna()
        & (f["n_unknown_on_invoice_edges"] == 0)
        & (f["n_unknown_off_invoice_edges"] == 0)
    )
    f["certified"] = f.groupby("transaction_id")["line_certified"].transform("all")

    # Why not, in words -- these become Confidence.reasons on the contract.
    f["uncertified_reason"] = np.select(
        [
            f["certified"],
            f["list_price"].isna(),
            f["unit_cost"].isna(),
            (f["n_unknown_on_invoice_edges"] > 0) | (f["n_unknown_off_invoice_edges"] > 0),
        ],
        [
            "",
            "product has no list_price",
            "product has no unit_cost",
            "discount applied with unknown amount",
        ],
        default="another line in this transaction is incomplete",
    )

    f["period"] = f["date"].dt.to_period("M").astype(str)
    return f


# ==========================================================================
# Totals
# ==========================================================================
def totals_from(facts: pd.DataFrame, certified_only: bool = True) -> pd.Series:
    """
    Sum the fact table into a single waterfall.

    Certified rows only by default: mixing complete and incomplete rows makes
    the steps stop reconciling, which is worse than a smaller, honest number.
    """
    scope = facts[facts["certified"]] if certified_only else facts
    n_txn_all = facts["transaction_id"].nunique()
    n_txn_cert = facts.loc[facts["certified"], "transaction_id"].nunique()

    t = pd.Series({s: scope[s].sum(min_count=1) for s in STEPS})
    t["n_transactions"] = float(n_txn_all)
    t["n_certified"] = float(n_txn_cert)
    t["n_excluded_incomplete"] = float(n_txn_all - n_txn_cert)
    t["coverage_pct"] = 100.0 * n_txn_cert / n_txn_all if n_txn_all else np.nan
    t["n_lines"] = float(len(scope))
    t["pocket_margin_pct"] = (
        100.0 * t["pocket_margin"] / t["pocket_price"] if t["pocket_price"] else np.nan
    )
    t["price_realisation_pct"] = (
        100.0 * t["pocket_price"] / t["list_value"] if t["list_value"] else np.nan
    )
    return t


def flags_from(facts: pd.DataFrame) -> pd.DataFrame:
    """Data-quality findings, one row per issue type, with blast radius."""

    def n_txn(mask) -> int:
        return int(facts.loc[mask, "transaction_id"].nunique())

    unknown_edges = facts["n_unknown_on_invoice_edges"] + facts["n_unknown_off_invoice_edges"]
    return pd.DataFrame(
        [
            {
                "flag": "product missing list_price",
                "n_lines": int(facts["list_price"].isna().sum()),
                "n_transactions": n_txn(facts["list_price"].isna()),
                "consequence": "list value & price realisation UNAVAILABLE",
            },
            {
                "flag": "product missing unit_cost",
                "n_lines": int(facts["unit_cost"].isna().sum()),
                "n_transactions": n_txn(facts["unit_cost"].isna()),
                "consequence": "COGS & pocket margin UNAVAILABLE",
            },
            {
                "flag": "discount applied with unknown amount",
                "n_lines": int((unknown_edges > 0).sum()),
                "n_transactions": n_txn(unknown_edges > 0),
                "consequence": "leakage understated; not certified",
            },
            {
                "flag": "line priced above list",
                "n_lines": int((facts["unit_price"] > facts["list_price"]).sum()),
                "n_transactions": n_txn(facts["unit_price"] > facts["list_price"]),
                "consequence": "negative discount -- check list price maintenance",
            },
            {
                "flag": "negative pocket margin",
                "n_lines": int((facts["pocket_margin"] < 0).sum()),
                "n_transactions": n_txn(facts["pocket_margin"] < 0),
                "consequence": "selling below cost -- a real finding, not an error",
            },
        ]
    )


# ==========================================================================
# Entry points
# ==========================================================================
def compute(src: GraphSource, transactions: pd.DataFrame | None = None) -> Waterfall:
    """Full waterfall over the snapshot (or a subset of transactions)."""
    facts = build_facts(src, transactions)
    return Waterfall(
        facts=facts,
        totals=totals_from(facts),
        flags=flags_from(facts),
        snapshot=src.snapshot_info(),
    )


def for_customer(src: GraphSource, customer_id: str) -> Waterfall:
    """Waterfall for one customer."""
    txn = src.customer_transactions(customer_id)
    if txn.empty:
        raise ValueError(f"No transactions found for customer {customer_id!r}")
    return compute(src, txn)


# ==========================================================================
# Presentation
# ==========================================================================
def format_waterfall(t: pd.Series, indent: str = "  ") -> str:
    """Render a totals Series as the familiar stepped waterfall."""
    base = t.get("list_value")

    def money(x):
        return "           n/a" if pd.isna(x) else f"{x:>14,.2f}"

    def share(x):
        if pd.isna(base) or not base or pd.isna(x):
            return ""
        return f"   ({100 * x / base:5.1f}% of list)"

    rows = [
        ("List value", "list_value"),
        ("- negotiated off-list", "negotiated_off_list"),
        ("= Gross (invoiced) value", "gross_value"),
        ("- on-invoice discounts", "on_invoice_discount"),
        ("= Invoice price", "invoice_price"),
        ("- off-invoice leakage", "off_invoice_leakage"),
        ("= Pocket price", "pocket_price"),
        ("- COGS", "cogs"),
        ("= Pocket margin", "pocket_margin"),
    ]
    out = []
    if not pd.isna(t.get("coverage_pct", np.nan)):
        out += [
            f"{indent}Coverage: {int(t.get('n_certified', 0))}/{int(t.get('n_transactions', 0))} "
            f"transactions fully decomposable ({t['coverage_pct']:.1f}%). Certified only below.",
            "",
        ]
    for label, key in rows:
        out.append(f"{indent}{label:<28}{money(t.get(key))}{share(t.get(key))}")
    out += [
        f"{indent}{'Pocket margin %':<28}{t.get('pocket_margin_pct', float('nan')):>14.2f}%",
        f"{indent}{'Price realisation':<28}{t.get('price_realisation_pct', float('nan')):>14.2f}%   (pocket / list)",
    ]
    return "\n".join(out)
