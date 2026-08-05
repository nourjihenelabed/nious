"""
Engine tests (D2): waterfall arithmetic and aggregation.

The three properties that must never regress:

  1. The waterfall matches an independent hand computation within 0.01%.
  2. Aggregating to any dimension sums back to the grand total exactly.
  3. Missing is not zero, and a partial sum is not a result.

Run:  pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import pipeline  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource  # noqa: E402


@pytest.fixture(scope="module")
def src() -> CsvGraphSource:
    return CsvGraphSource()


@pytest.fixture(scope="module")
def w(src) -> wf.Waterfall:
    return wf.compute(src)


# ==========================================================================
# 1. Arithmetic, against an independent implementation
# ==========================================================================
def _hand_compute(src, txn_id: str) -> dict:
    """
    Deliberately naive, loop-based re-implementation, so a mistake in the
    vectorised engine cannot hide inside an identical mistake here.
    """
    lines = src.transaction_lines()
    lines = lines[lines["transaction_id"] == txn_id]
    prod = src.products().set_index("product_id")

    list_value = cogs = gross = 0.0
    for _, ln in lines.iterrows():
        p = prod.loc[ln["product_id"]]
        list_value += ln["quantity"] * p["list_price"]
        cogs += ln["quantity"] * p["unit_cost"]
        gross += ln["line_total"]

    td = src.transaction_discounts()
    td = td[td["transaction_id"] == txn_id]
    disc = src.discounts().set_index("discount_id")
    on = off = 0.0
    for _, row in td.iterrows():
        amt = row["discount_amount_applied"]
        if disc.loc[row["discount_id"], "type"] == "on-invoice":
            on += amt
        else:
            off += amt

    invoice = gross - on
    pocket = invoice - off
    return {
        "list_value": list_value,
        "gross_value": gross,
        "on_invoice_discount": on,
        "invoice_price": invoice,
        "off_invoice_leakage": off,
        "pocket_price": pocket,
        "cogs": cogs,
        "pocket_margin": pocket - cogs,
    }


def test_waterfall_matches_hand_computation(src, w):
    """D2 acceptance criterion: agreement within 0.01%."""
    by_txn = w.by_transaction().set_index("transaction_id")
    certified = by_txn[by_txn["status"] == "complete"]
    sample = list(certified.index[:5]) + list(certified.index[-5:])

    for txn_id in sample:
        expected = _hand_compute(src, txn_id)
        row = certified.loc[txn_id]
        for key, exp in expected.items():
            tol = max(abs(exp) * 1e-4, 0.01)  # 0.01% or one cent, whichever larger
            assert abs(row[key] - exp) <= tol, f"{txn_id}.{key}: engine={row[key]} hand={exp}"


def test_waterfall_steps_reconcile(w):
    """Each level equals the previous level minus its deduction, exactly."""
    f = w.facts
    assert (f["invoice_price"] - (f["gross_value"] - f["on_invoice_discount"])).abs().max() < 0.01
    assert (f["pocket_price"] - (f["invoice_price"] - f["off_invoice_leakage"])).abs().max() < 0.01
    m = f.dropna(subset=["cogs"])
    assert (m["pocket_margin"] - (m["pocket_price"] - m["cogs"])).abs().max() < 0.01


def test_totals_reconcile(w):
    t = w.totals
    assert abs((t["gross_value"] - t["on_invoice_discount"]) - t["invoice_price"]) < 0.01
    assert abs((t["invoice_price"] - t["off_invoice_leakage"]) - t["pocket_price"]) < 0.01
    assert abs((t["pocket_price"] - t["cogs"]) - t["pocket_margin"]) < 0.01
    assert abs((t["list_value"] - t["negotiated_off_list"]) - t["gross_value"]) < 0.01


# ==========================================================================
# 2. The allocation is exact, and aggregation is faithful
# ==========================================================================
def test_line_rollup_equals_transaction(src, w):
    """
    Pro-rata allocation must be lossless: summing allocated line discounts back
    to the transaction must reproduce the original transaction-level totals.

    This is the guarantee that lets the whole engine live on one line-grain
    fact table. If it ever fails, every aggregate is suspect.
    """
    rolled = w.facts.groupby("transaction_id")[
        ["on_invoice_discount", "off_invoice_leakage"]
    ].sum(min_count=1)

    td = src.transaction_discounts().merge(
        src.discounts()[["discount_id", "type"]], on="discount_id", how="left"
    )
    for kind, col in (("on-invoice", "on_invoice_discount"), ("off-invoice", "off_invoice_leakage")):
        direct = (
            td[td["type"] == kind]
            .groupby("transaction_id")["discount_amount_applied"]
            .sum(min_count=1)
            .reindex(rolled.index)
            .fillna(0)
        )
        diff = (rolled[col].fillna(0) - direct).abs()
        assert diff.max() < 0.01, f"{col} allocation lost {diff.max():.4f}"


@pytest.mark.parametrize("dimension", list(wf.DIMENSIONS) + ["transaction_id"])
def test_aggregation_reconciles_with_grand_total(w, dimension):
    """Any groupby must sum back to the grand total -- catches join fanout."""
    rec = agg.reconciles_with_total(w, dimension)
    assert rec["ok"].all(), f"{dimension} does not reconcile:\n{rec}"


def test_aggregation_uses_sums_not_means_of_ratios(w):
    """
    Pocket margin % for a group must be sum(margin)/sum(pocket), not the mean of
    per-deal percentages. The two differ whenever deal sizes differ, and the
    mean-of-ratios version silently over-weights small deals.
    """
    a = agg.aggregate(w, "segment").set_index("segment")
    facts = w.facts[w.facts["certified"]]
    for seg, row in a.iterrows():
        sub = facts[facts["segment"] == seg]
        expected = 100 * sub["pocket_margin"].sum() / sub["pocket_price"].sum()
        assert abs(row["pocket_margin_pct"] - expected) < 1e-6

        mean_of_ratios = (100 * sub["pocket_margin"] / sub["pocket_price"]).mean()
        assert abs(row["pocket_margin_pct"] - mean_of_ratios) > 1e-6, (
            "value-weighted and mean-of-ratios coincide -- test is not discriminating"
        )


def test_coverage_denominator_is_the_full_population(w):
    """
    Coverage must be certified/ALL, not certified/certified. Getting this wrong
    makes every group look 100% complete, which is the exact lie we are guarding
    against.
    """
    a = agg.aggregate(w, "segment")
    assert (a["n_transactions"] >= a["n_transactions_certified"]).all()
    assert (a["coverage_pct"] < 100).any(), "expected some incomplete data in this snapshot"
    assert a["n_transactions"].sum() == w.facts["transaction_id"].nunique()


def test_thin_groups_abstain_rather_than_vanish(w):
    """A group below the threshold is flagged, not dropped."""
    loose = agg.aggregate(w, "customer", min_transactions=0)
    strict = agg.aggregate(w, "customer", min_transactions=15)
    assert len(loose) == len(strict), "groups disappeared instead of abstaining"
    assert (strict["status"] == "abstained").any()
    assert strict.loc[strict["status"] == "abstained", "pocket_margin"].isna().all()


# ==========================================================================
# 3. Missing is not zero
# ==========================================================================
def test_missing_inputs_are_unavailable_not_zero(src, w):
    """Products with NULL list_price / unit_cost must produce NaN, never 0."""
    prod = src.products()
    bad_list = set(prod.loc[prod["list_price"].isna(), "product_id"])
    bad_cost = set(prod.loc[prod["unit_cost"].isna(), "product_id"])
    assert bad_list or bad_cost, "fixture assumption broken"

    f = w.facts
    assert f.loc[f["product_id"].isin(bad_list), "list_value"].isna().all()
    assert f.loc[f["product_id"].isin(bad_cost), "cogs"].isna().all()
    assert (f.loc[f["product_id"].isin(bad_list), "list_value"] == 0).sum() == 0


def test_partial_transactions_are_not_certified(src, w):
    """
    One bad line poisons the whole transaction: a partially-known order yields a
    partial sum, and a partial sum is not a result.
    """
    prod = src.products()
    bad = set(prod.loc[prod["list_price"].isna() | prod["unit_cost"].isna(), "product_id"])
    lines = src.transaction_lines()
    affected = set(lines.loc[lines["product_id"].isin(bad), "transaction_id"])

    f = w.facts
    assert not f.loc[f["transaction_id"].isin(affected), "certified"].any()
    # and every affected row explains itself
    assert (f.loc[f["transaction_id"].isin(affected), "uncertified_reason"] != "").all()


def test_undiscounted_sales_are_zero_not_unknown(src, w):
    """
    A transaction with no DISCOUNTED_BY edges genuinely had no discount -- that
    is a true zero, and it must stay certified. (Regression: these were briefly
    read as 'amount unknown' and dropped out of the certified book.)
    """
    td = set(src.transaction_discounts()["transaction_id"])
    f = w.facts
    no_disc = f[~f["transaction_id"].isin(td)]
    assert len(no_disc), "fixture assumption broken: expected some undiscounted sales"
    assert (no_disc["on_invoice_discount"] == 0).all()
    assert (no_disc["off_invoice_leakage"] == 0).all()
    assert no_disc["on_invoice_discount"].notna().all()


def test_certified_totals_exclude_incomplete(w):
    t = w.totals
    assert t["n_certified"] + t["n_excluded_incomplete"] == t["n_transactions"]
    assert t["n_certified"] < t["n_transactions"], "expected exclusions in this snapshot"


# ==========================================================================
# 4. The pipeline
# ==========================================================================
def test_pipeline_runs_and_self_checks(src):
    run = pipeline.run(src, track=False)
    assert run.validation.ok
    assert run.reconciliation["ok"].all()
    assert not run.contract_violations
    assert run.ok
    # keyed by the dimension you asked for, not by the underlying column name
    assert set(run.aggregations) == set(pipeline.DEFAULT_GRAINS)
    for name, df in run.aggregations.items():
        assert len(df), f"{name} aggregation is empty"


def test_pipeline_refuses_a_broken_snapshot(src, tmp_path):
    from pricing import config
    from pricing.graph.validation import SnapshotRejected

    for fname in config.CSV_FILES.values():
        (tmp_path / fname).write_bytes((Path(src.data_dir) / fname).read_bytes())
    lines = pd.read_csv(tmp_path / "transaction_lines.csv")
    lines.loc[0, "product_id"] = "PRD-GHOST"
    lines.to_csv(tmp_path / "transaction_lines.csv", index=False)

    with pytest.raises(SnapshotRejected):
        pipeline.run(CsvGraphSource(tmp_path), track=False)
