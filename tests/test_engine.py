"""Engine tests (D2): waterfall arithmetic and aggregation on the real snapshot."""

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
    """The synthetic snapshot the module runs against."""
    return CsvGraphSource()


@pytest.fixture(scope="module")
def w(src) -> wf.Waterfall:
    """The waterfall over the whole snapshot."""
    return wf.compute(src)


def _hand_compute(src, txn_id: str) -> dict:
    """A naive loop-based re-implementation, so a bug cannot hide in both versions."""
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
            tol = max(abs(exp) * 1e-4, 0.01)
            assert abs(row[key] - exp) <= tol, f"{txn_id}.{key}: engine={row[key]} hand={exp}"


def test_waterfall_steps_reconcile(w):
    """Each level equals the previous level minus its deduction, exactly."""
    k = w.keys
    assert (k["invoice_price"] - (k["gross_value"] - k["on_invoice_discount"])).abs().max() < 0.01
    assert (k["pocket_price"] - (k["invoice_price"] - k["off_invoice_leakage"])).abs().max() < 0.01
    m = k.dropna(subset=["cogs"])
    assert (m["pocket_margin"] - (m["pocket_price"] - m["cogs"])).abs().max() < 0.01


def test_line_grain_steps_reconcile_on_the_fact_table(w):
    """The part of the chain that is knowable per line still reconciles per line."""
    f = w.facts
    m = f.dropna(subset=["list_value", "gross_value"])
    assert (m["negotiated_off_list"] - (m["list_value"] - m["gross_value"])).abs().max() < 0.01


def test_totals_reconcile(w):
    """The same, at the level of the grand total."""
    t = w.totals
    assert abs((t["gross_value"] - t["on_invoice_discount"]) - t["invoice_price"]) < 0.01
    assert abs((t["invoice_price"] - t["off_invoice_leakage"]) - t["pocket_price"]) < 0.01
    assert abs((t["pocket_price"] - t["cogs"]) - t["pocket_margin"]) < 0.01
    assert abs((t["list_value"] - t["negotiated_off_list"]) - t["gross_value"]) < 0.01


def test_document_grain_discounts_equal_their_edges_exactly(src, w):
    """No allocation means the discount total is the edge total, to the cent."""
    held = w.keys.set_index("transaction_id")[["on_invoice_discount", "off_invoice_leakage"]]

    td = src.transaction_discounts().merge(
        src.discounts()[["discount_id", "type"]], on="discount_id", how="left"
    )
    for kind, col in (("on-invoice", "on_invoice_discount"), ("off-invoice", "off_invoice_leakage")):
        direct = (
            td[td["type"] == kind]
            .groupby("transaction_id")["discount_amount_applied"]
            .sum(min_count=1)
            .reindex(held.index)
            .fillna(0)
        )
        diff = (held[col].fillna(0) - direct).abs()
        assert diff.max() < 0.01, f"{col} differs from its edges by {diff.max():.4f}"


def test_document_grain_steps_never_reach_the_line_table(w):
    """The engine must not carry an order-grain discount at line grain at all."""
    for step in ("on_invoice_discount", "off_invoice_leakage", "pocket_price", "pocket_margin"):
        assert step not in w.facts.columns, f"{step} leaked into the line-grain fact table"
        assert step in w.keys.columns


def test_product_grain_abstains_on_document_grain_steps(w):
    """Slicing by product suppresses the steps the data cannot place there."""
    by_product = agg.aggregate(w, "product")
    by_customer = agg.aggregate(w, "customer")

    assert by_product["on_invoice_discount"].isna().all()
    assert by_product["pocket_margin"].isna().all()
    assert by_product["list_value"].notna().any(), "line-grain steps must survive"
    assert by_product["grain_note"].iloc[0], "suppression must state its reason"

    assert by_customer["on_invoice_discount"].notna().any()
    assert by_customer["grain_note"].iloc[0] == ""


@pytest.mark.parametrize("dimension", list(wf.DIMENSIONS) + ["transaction_id"])
def test_aggregation_reconciles_with_grand_total(w, dimension):
    """Any groupby must sum back to the grand total -- catches join fanout."""
    rec = agg.reconciles_with_total(w, dimension)
    assert rec["ok"].all(), f"{dimension} does not reconcile:\n{rec}"


def test_aggregation_uses_sums_not_means_of_ratios(w):
    """Group margin % must be sum(margin)/sum(pocket), not the mean of per-deal rates."""
    a = agg.aggregate(w, "segment").set_index("segment")
    docs = w.keys[w.keys["certified"]]
    for seg, row in a.iterrows():
        sub = docs[docs["segment"] == seg]
        expected = 100 * sub["pocket_margin"].sum() / sub["pocket_price"].sum()
        assert abs(row["pocket_margin_pct"] - expected) < 1e-6

        mean_of_ratios = (100 * sub["pocket_margin"] / sub["pocket_price"]).mean()
        assert abs(row["pocket_margin_pct"] - mean_of_ratios) > 1e-6, (
            "value-weighted and mean-of-ratios coincide -- test is not discriminating"
        )


def test_coverage_denominator_is_the_full_population(w):
    """Coverage is certified/ALL; certified/certified would always read 100%."""
    a = agg.aggregate(w, "segment")
    assert (a["n_transactions"] >= a["n_transactions_certified"]).all()
    assert (a["coverage_pct"] < 100).any(), "expected some incomplete data in this snapshot"
    assert a["n_transactions"].sum() == w.keys["transaction_id"].nunique()


def test_thin_groups_abstain_rather_than_vanish(w):
    """A group below the threshold is flagged, not dropped."""
    loose = agg.aggregate(w, "customer", min_transactions=0)
    strict = agg.aggregate(w, "customer", min_transactions=15)
    assert len(loose) == len(strict), "groups disappeared instead of abstaining"
    assert (strict["status"] == "abstained").any()
    assert strict.loc[strict["status"] == "abstained", "pocket_margin"].isna().all()


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
    """One bad line poisons the whole transaction, and the row says why."""
    prod = src.products()
    bad = set(prod.loc[prod["list_price"].isna() | prod["unit_cost"].isna(), "product_id"])
    lines = src.transaction_lines()
    affected = set(lines.loc[lines["product_id"].isin(bad), "transaction_id"])

    f = w.facts
    assert not f.loc[f["transaction_id"].isin(affected), "certified"].any()
    assert (f.loc[f["transaction_id"].isin(affected), "uncertified_reason"] != "").all()


def test_undiscounted_sales_are_zero_not_unknown(src, w):
    """A transaction with no DISCOUNTED_BY edge had no discount: a true zero."""
    td = set(src.transaction_discounts()["transaction_id"])
    no_disc = w.keys[~w.keys["transaction_id"].isin(td)]
    assert len(no_disc), "fixture assumption broken: expected some undiscounted sales"
    assert (no_disc["on_invoice_discount"] == 0).all()
    assert (no_disc["off_invoice_leakage"] == 0).all()
    assert no_disc["on_invoice_discount"].notna().all()


def test_certified_totals_exclude_incomplete(w):
    """Headline totals cover certified transactions only, and say how many."""
    t = w.totals
    assert t["n_certified"] + t["n_excluded_incomplete"] == t["n_transactions"]
    assert t["n_certified"] < t["n_transactions"], "expected exclusions in this snapshot"


def test_pipeline_runs_and_self_checks(src):
    """The one entry point validates, computes, reconciles and emits a clean contract."""
    run = pipeline.run(src, track=False)
    assert run.validation.ok
    assert run.reconciliation["ok"].all()
    assert not run.contract_violations
    assert run.ok
    assert set(run.aggregations) == set(pipeline.DEFAULT_GRAINS)
    for name, df in run.aggregations.items():
        assert len(df), f"{name} aggregation is empty"


def test_pipeline_refuses_a_broken_snapshot(src, tmp_path):
    """A dangling product reference must stop the run, not be quietly tolerated."""
    from pricing import config
    from pricing.graph.validation import SnapshotRejected

    for fname in config.CSV_FILES.values():
        (tmp_path / fname).write_bytes((Path(src.data_dir) / fname).read_bytes())
    lines = pd.read_csv(tmp_path / "transaction_lines.csv")
    lines.loc[0, "product_id"] = "PRD-GHOST"
    lines.to_csv(tmp_path / "transaction_lines.csv", index=False)

    with pytest.raises(SnapshotRejected):
        pipeline.run(CsvGraphSource(tmp_path), track=False)
