"""D2 acceptance: the engine against the hand-computed reference dataset."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.contracts import schema as C  # noqa: E402
from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import result as result_builder  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource, validation  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"

PROFILE = get_profile("b2b_industrial")
STEPS = list(wf.STEPS)

#: Steps this schema records against the order rather than the line.
DOCUMENT_GRAIN_STEPS = [s for s in STEPS if PROFILE.step(s).grain == "key"]


def reportable(dimension: str) -> list[str]:
    """The steps a hand computation can legitimately expect at this grain."""
    if PROFILE.key_safe(PROFILE.resolve(dimension)):
        return STEPS
    return [s for s in STEPS if s not in DOCUMENT_GRAIN_STEPS]


KEY_COLUMN = {
    "customer": "customer_id",
    "product": "product_id",
    "segment": "segment",
    "channel": "channel_id",
    "region": "region_id",
    "period": "period",
}


def tolerance(expected: float) -> float:
    """The D2 criterion as a number: 0.01% of the value, floored at one cent."""
    return max(abs(expected) * 1e-4, 0.01)


@pytest.fixture(scope="module")
def src() -> CsvGraphSource:
    """The eleven-transaction reference snapshot."""
    return CsvGraphSource(REFERENCE_DIR / "snapshot")


@pytest.fixture(scope="module")
def w(src) -> wf.Waterfall:
    """The waterfall the engine computes over that snapshot."""
    return wf.compute(src)


@pytest.fixture(scope="module")
def expected_transactions() -> pd.DataFrame:
    """The hand-computed answer, one row per transaction."""
    df = pd.read_csv(REFERENCE_DIR / "expected_transactions.csv")
    return df.set_index("transaction_id")


@pytest.fixture(scope="module")
def expected_aggregates() -> pd.DataFrame:
    """The hand-computed answer, one row per group per dimension."""
    return pd.read_csv(REFERENCE_DIR / "expected_aggregates.csv", dtype={"key": str})


def test_reference_snapshot_passes_the_entry_gate(src):
    """The fixture is degraded on purpose but structurally sound, so it must run."""
    report = validation.validate(src)
    assert report.ok, f"reference snapshot has blocking findings:\n{report}"
    checks = {f.check for f in report.warnings}
    assert {"list price coverage", "cost coverage", "discount amounts"} <= checks
    assert "currency" in checks, "two currencies with no FX rate should be flagged"


def test_reference_expectations_are_internally_consistent(expected_aggregates):
    """The oracle checks itself: every dimension must sum to the same grand total."""
    total = expected_aggregates[expected_aggregates["dimension"] == "TOTAL"].iloc[0]
    for dim in KEY_COLUMN:
        rows = expected_aggregates[expected_aggregates["dimension"] == dim]
        assert len(rows), f"no expectations recorded for {dim}"
        for step in reportable(dim):
            got = rows[step].sum(min_count=1)
            assert abs(got - total[step]) < 0.01, (
                f"hand-computed {dim} totals sum to {got}, grand total is {total[step]}"
            )
        for step in set(STEPS) - set(reportable(dim)):
            assert rows[step].isna().all(), (
                f"{dim} is finer than {step}'s grain, so no value can be expected for it"
            )


def test_every_transaction_matches_the_hand_computation(w, expected_transactions):
    """D2: list -> pocket margin agrees with the manual reference within 0.01%."""
    got = w.by_transaction(certified_only=False).set_index("transaction_id")
    assert set(got.index) == set(expected_transactions.index)

    for txn_id, exp in expected_transactions.iterrows():
        row = got.loc[txn_id]
        for step in STEPS:
            want = exp[step]
            if pd.isna(want):
                assert pd.isna(row[step]), (
                    f"{txn_id}.{step}: expected UNAVAILABLE, engine produced {row[step]}"
                )
            else:
                assert abs(row[step] - want) <= tolerance(want), (
                    f"{txn_id}.{step}: engine={row[step]} reference={want}"
                )


def test_certification_matches_the_hand_computation(w, expected_transactions):
    """Which transactions are fully decomposable is itself part of the answer."""
    certified = w.facts.groupby("transaction_id")["certified"].first()
    for txn_id, exp in expected_transactions.iterrows():
        assert bool(certified[txn_id]) is (exp["certified"] == "yes"), (
            f"{txn_id}: certified={certified[txn_id]}, reference says {exp['certified']}"
        )


def test_uncertified_transactions_state_the_expected_reason(w, expected_transactions):
    """An exclusion nobody can explain is indistinguishable from a bug."""
    facts = w.facts
    for txn_id, exp in expected_transactions.iterrows():
        if exp["certified"] == "yes":
            continue
        reasons = set(facts.loc[facts["transaction_id"] == txn_id, "uncertified_reason"])
        assert exp["expected_reason"] in reasons, (
            f"{txn_id}: reasons {reasons} do not include {exp['expected_reason']!r}"
        )


def test_grand_total_matches_the_hand_computation(w, expected_aggregates):
    """The nine headline numbers, against the arithmetic on paper."""
    total = expected_aggregates[expected_aggregates["dimension"] == "TOTAL"].iloc[0]
    for step in STEPS:
        assert abs(w.totals[step] - total[step]) <= tolerance(total[step]), (
            f"total {step}: engine={w.totals[step]} reference={total[step]}"
        )
    assert w.totals["n_transactions"] == total["n_transactions"]
    assert w.totals["n_certified"] == total["n_transactions_certified"]


def test_headline_ratios_match_the_hand_computation(w):
    """23.23% margin and 74.16% realisation, both computed from sums."""
    assert abs(w.totals["pocket_margin_pct"] - (100 * 1516 / 6526)) < 1e-6
    assert abs(w.totals["price_realisation_pct"] - (100 * 6526 / 8800)) < 1e-6


@pytest.mark.parametrize("dimension", sorted(KEY_COLUMN))
def test_aggregate_matches_the_hand_computation(w, expected_aggregates, dimension):
    """Every required D2 grain, against group totals worked out by hand."""
    key_col = KEY_COLUMN[dimension]
    got = agg.aggregate(w, dimension).set_index(key_col)
    exp = expected_aggregates[expected_aggregates["dimension"] == dimension].set_index("key")

    assert set(got.index) == set(exp.index), (
        f"{dimension}: engine groups {sorted(got.index)} vs reference {sorted(exp.index)}"
    )

    for step in set(STEPS) - set(reportable(dimension)):
        assert got[step].isna().all(), f"{dimension}.{step} should abstain on grain"
        assert got["grain_note"].iloc[0], "an abstention must state its reason"

    for key, row in exp.iterrows():
        for step in reportable(dimension):
            want = row[step]
            if pd.isna(want):
                assert pd.isna(got.loc[key, step]), (
                    f"{dimension}[{key}].{step}: expected UNAVAILABLE, got {got.loc[key, step]}"
                )
            else:
                assert abs(got.loc[key, step] - want) <= tolerance(want), (
                    f"{dimension}[{key}].{step}: engine={got.loc[key, step]} reference={want}"
                )
        assert got.loc[key, "n_transactions"] == row["n_transactions"]
        assert got.loc[key, "n_transactions_certified"] == row["n_transactions_certified"]


@pytest.mark.parametrize("dimension", sorted(KEY_COLUMN))
def test_every_dimension_reconciles_with_the_grand_total(w, dimension):
    """A groupby that does not sum back to the total has fanned out a join."""
    rec = agg.reconciles_with_total(w, dimension)
    assert rec["ok"].all(), f"{dimension} does not reconcile:\n{rec}"


def test_coverage_is_measured_against_the_full_population(w):
    """CUS-A3 has three transactions and one certified: 33.33%, not 100%."""
    got = agg.aggregate(w, "customer").set_index("customer_id")
    assert got.loc["CUS-A3", "n_transactions"] == 3
    assert got.loc["CUS-A3", "n_transactions_certified"] == 1
    assert abs(got.loc["CUS-A3", "coverage_pct"] - 33.33) < 0.01
    assert abs(got.loc["CUS-A2", "coverage_pct"] - 100.0) < 0.01


def test_groups_with_nothing_certified_appear_as_unavailable(w):
    """PRD-4 and PRD-5 sell, but nothing about them can be decomposed."""
    got = agg.aggregate(w, "product").set_index("product_id")
    for product_id in ("PRD-4", "PRD-5"):
        assert product_id in got.index, f"{product_id} vanished instead of abstaining"
        assert got.loc[product_id, "status"] == "unavailable"
        assert pd.isna(got.loc[product_id, "pocket_margin"])
        assert got.loc[product_id, "n_transactions"] > 0


def test_the_order_discount_is_not_split_across_its_lines(w):
    """R04 has two lines and one 120 discount. The engine must not divide it."""
    lines = w.facts[w.facts["transaction_id"] == "TRX-R04"]
    assert len(lines) == 2
    assert "on_invoice_discount" not in lines.columns
    assert "pocket_price" not in lines.columns

    order = w.keys.set_index("transaction_id").loc["TRX-R04"]
    assert abs(order["on_invoice_discount"] - 120.0) < 0.01
    assert abs(order["off_invoice_leakage"] - 72.0) < 0.01
    assert abs(order["pocket_price"] - 1008.0) < 0.01

    by_product = agg.aggregate(w, "product")
    assert by_product["on_invoice_discount"].isna().all()


def test_order_grain_totals_match_the_hand_computation(w, expected_transactions):
    """The order-grain steps must equal the hand-computed order totals exactly."""
    held = w.keys.set_index("transaction_id")
    for txn_id, exp in expected_transactions.iterrows():
        for step in ("on_invoice_discount", "off_invoice_leakage"):
            if pd.isna(exp[step]):
                continue
            assert abs(held.loc[txn_id, step] - exp[step]) < 0.01


def test_missing_product_fields_are_unavailable_not_zero(w):
    """A missing list price or cost blanks its step; the others still compute."""
    facts = w.facts.set_index(["transaction_id", "product_id"])
    held = w.keys.set_index("transaction_id")

    assert pd.isna(facts.loc[("TRX-R06", "PRD-4"), "list_value"])
    assert pd.isna(facts.loc[("TRX-R06", "PRD-4"), "negotiated_off_list"])
    assert abs(held.loc["TRX-R06", "pocket_price"] - 450.0) < 0.01

    assert pd.isna(facts.loc[("TRX-R07", "PRD-5"), "cogs"])
    assert pd.isna(held.loc["TRX-R07", "pocket_margin"])
    assert abs(facts.loc[("TRX-R07", "PRD-5"), "list_value"] - 800.0) < 0.01


def test_unknown_discount_amount_blocks_only_the_steps_below_it(w):
    """R08's leakage is unknown, so pocket price is too -- but invoice price holds."""
    row = w.keys.set_index("transaction_id").loc["TRX-R08"]
    assert abs(row["gross_value"] - 850.0) < 0.01
    assert abs(row["on_invoice_discount"] - 85.0) < 0.01
    assert abs(row["invoice_price"] - 765.0) < 0.01
    assert pd.isna(row["off_invoice_leakage"])
    assert pd.isna(row["pocket_price"])
    assert row["n_unknown_off_invoice_leakage"] == 1
    assert not row["certified"]


def test_no_discount_edge_means_a_true_zero(w):
    """R01 had no concessions at all: a zero we know, and it stays certified."""
    row = w.keys.set_index("transaction_id").loc["TRX-R01"]
    assert row["on_invoice_discount"] == 0.0
    assert row["off_invoice_leakage"] == 0.0
    assert row["certified"]


def test_one_bad_line_uncertifies_the_whole_transaction(w):
    """R09's first line is perfectly good; a partial sum is still not a result."""
    lines = w.facts[w.facts["transaction_id"] == "TRX-R09"].set_index("product_id")
    assert lines.loc["PRD-1", "line_certified"]
    assert not lines.loc["PRD-5", "line_certified"]
    assert not lines["certified"].any()


def test_negative_margin_is_reported_not_suppressed(w):
    """R05 sold below cost, which is the module's whole reason for existing."""
    got = w.by_transaction().set_index("transaction_id")
    assert got.loc["TRX-R05", "pocket_margin"] < 0
    flags = w.flags.set_index("flag")
    assert flags.loc["negative pocket margin", "n_transactions"] >= 1


def test_step_availability_reports_the_reference_gaps(w):
    """The per-step statement of what the book can and cannot report."""
    avail = w.availability().set_index("step")

    assert avail.loc["list_value", "n_lines_unavailable"] == 1
    assert avail.loc["cogs", "n_lines_unavailable"] == 2
    assert avail.loc["off_invoice_leakage", "n_lines_unavailable"] == 1
    assert avail.loc["pocket_margin", "n_lines_unavailable"] == 4
    assert avail.loc["gross_value", "n_lines_unavailable"] == 0
    assert avail.loc["gross_value", "n_zero"] == 2

    assert avail.loc["cogs", "grain"] == "line"
    assert avail.loc["pocket_margin", "grain"] == "key"

    assert avail.loc["cogs", "n_transactions_unavailable"] == 2
    assert avail.loc["pocket_margin", "n_transactions_unavailable"] == 3
    assert avail.loc["list_value", "n_transactions_unavailable"] == 1


def test_free_of_charge_order_has_no_margin_percentage(w):
    """R11's pocket price is a true zero, so its margin percentage does not exist."""
    got = w.by_transaction().set_index("transaction_id")
    row = got.loc["TRX-R11"]

    assert row["pocket_price"] == 0.0
    assert abs(row["pocket_margin"] - (-160.0)) < 0.01
    assert pd.isna(row["pocket_margin_pct"]), f"expected NaN, got {row['pocket_margin_pct']}"
    assert not math.isinf(row["pocket_margin_pct"])
    assert abs(row["price_realisation_pct"]) < 1e-9


def test_no_aggregate_anywhere_produces_an_infinity(w):
    """Every ratio on every dimension is finite or absent, never +/-inf."""
    ratio_columns = [
        "pocket_margin_pct", "price_realisation_pct", "on_invoice_discount_pct",
        "off_invoice_leakage_pct", "total_leakage_pct",
    ]
    for dimension in [*KEY_COLUMN, "transaction"]:
        a = agg.aggregate(w, dimension)
        for col in ratio_columns:
            bad = a[col].apply(lambda v: not pd.isna(v) and not math.isfinite(v))
            assert not bad.any(), f"{dimension}.{col} contains an infinity"


def test_reference_payload_is_valid_json(w):
    """The payload Track B receives must load in any JSON parser, not just Python's."""
    result = result_builder.build(
        w, grains=(["customer", "currency"], ["segment", "currency"]), min_transactions=0
    )
    payload = result.to_json()
    assert "Infinity" not in payload
    assert "NaN" not in payload
    json.loads(payload)
    assert not C.validate(result), "\n".join(C.validate(result))


def test_contract_rejects_a_non_finite_number():
    """The finite-number guard must actually reject, not merely be present."""
    bad = C.EngineResult(
        provenance=C.Provenance("csv", "/nowhere", snapshot_hash="deadbeef"),
        waterfalls=(
            C.WaterfallRecord(
                grain="customer",
                key={"customer_id": "CUS-A3"},
                list_value=C.Money(300.0),
                negotiated_off_list=C.Money(300.0),
                gross_value=C.Money(0.0),
                on_invoice_discount=C.Money(0.0),
                invoice_price=C.Money(0.0),
                off_invoice_leakage=C.Money(0.0),
                pocket_price=C.Money(0.0),
                cogs=C.Money(160.0),
                pocket_margin=C.Money(-160.0),
                pocket_margin_pct=float("-inf"),
                price_realisation_pct=0.0,
                status=C.ResultStatus.COMPLETE,
                confidence=C.Confidence.from_score(0.9),
                evidence=C.Evidence(transaction_ids=("TRX-R11",)),
                n_transactions=1,
                n_certified=1,
            ),
        ),
    )
    problems = C.validate(bad)
    assert any("not a finite number" in p for p in problems)


def test_reference_records_carry_evidence_for_the_expected_deals(w):
    """Track B must be able to cite exactly the certified transactions behind a number."""
    result = result_builder.build(w, grains=(["customer", "currency"],), min_transactions=0)
    by_customer = {r.key["customer_id"]: r for r in result.waterfalls}

    assert set(by_customer["CUS-A1"].evidence.transaction_ids) == {
        "TRX-R01", "TRX-R02", "TRX-R05",
    }
    assert set(by_customer["CUS-B1"].evidence.transaction_ids) == {"TRX-R10"}
    assert "TRX-R08" not in by_customer["CUS-A1"].evidence.transaction_ids


def test_partial_coverage_never_claims_complete(w):
    """Only a group with every transaction decomposed may be reported COMPLETE."""
    result = result_builder.build(w, grains=(["customer", "currency"],), min_transactions=0)
    by_customer = {r.key["customer_id"]: r for r in result.waterfalls}

    assert by_customer["CUS-A2"].status is C.ResultStatus.COMPLETE
    assert by_customer["CUS-A1"].status is C.ResultStatus.PARTIAL
    assert by_customer["CUS-A3"].status is C.ResultStatus.PARTIAL
    for record in result.waterfalls:
        if record.confidence.band in (C.ConfidenceBand.LOW, C.ConfidenceBand.NONE):
            assert record.confidence.reasons
