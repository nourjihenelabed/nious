"""
Result-contract tests (D8 draft).

These are the tests Track B depends on. They check the promises we are making
to Mohamed, not the maths behind them:

  * every published number carries evidence he can cite
  * low confidence always comes with a stated reason
  * missing is expressed as UNAVAILABLE, never as 0
  * the payload round-trips to JSON

Run:  pytest -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.contracts import schema as C  # noqa: E402
from pricing.engine import result as result_builder  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource  # noqa: E402


@pytest.fixture(scope="module")
def w():
    return wf.compute(CsvGraphSource())


@pytest.fixture(scope="module")
def result(w) -> C.EngineResult:
    # currency in the grain -- the only publishable form (see below)
    return result_builder.build(
        w, grains=(["segment", "currency"], ["region", "currency"]), min_transactions=3
    )


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------
def test_money_distinguishes_missing_from_zero():
    zero = C.Money(0.0)
    missing = C.Money.unavailable("no unit cost")

    assert zero.amount == 0.0 and zero.status is C.ResultStatus.COMPLETE
    assert missing.amount is None and missing.status is C.ResultStatus.UNAVAILABLE
    # the truthiness guard: `if money:` must not silently mean `if amount:`
    assert bool(zero) is True
    assert bool(missing) is False
    assert missing.note


def test_confidence_bands():
    assert C.Confidence.from_score(0.95).band is C.ConfidenceBand.HIGH
    assert C.Confidence.from_score(0.60).band is C.ConfidenceBand.MEDIUM
    assert C.Confidence.from_score(0.20).band is C.ConfidenceBand.LOW
    assert C.Confidence.from_score(0.0).band is C.ConfidenceBand.NONE
    assert C.Confidence.from_score(5.0).score == 1.0  # clamped


def test_price_range_must_be_ordered():
    C.PriceRange(90, 100, 110)
    with pytest.raises(ValueError):
        C.PriceRange(110, 100, 90)


# --------------------------------------------------------------------------
# The promises to Track B
# --------------------------------------------------------------------------
def test_result_has_no_violations(result):
    problems = C.validate(result)
    assert not problems, "\n".join(problems)


def test_every_published_record_cites_evidence(result):
    """A number Track B cannot trace to records is a number it cannot publish."""
    for r in result.waterfalls:
        if r.status in (C.ResultStatus.COMPLETE, C.ResultStatus.PARTIAL):
            assert not r.evidence.is_empty(), f"{r.grain} {r.key} cites nothing"
            assert r.evidence.count() == r.n_certified or r.evidence.count() == 200


def test_low_confidence_always_explains_itself(result):
    for r in result.waterfalls:
        if r.confidence.band in (C.ConfidenceBand.LOW, C.ConfidenceBand.NONE):
            assert r.confidence.reasons, f"{r.key}: low confidence with no reason"


def test_partial_coverage_is_not_reported_as_complete(result):
    """No group in this snapshot is 100% decomposable, so none may claim COMPLETE."""
    assert result.waterfalls
    for r in result.waterfalls:
        if r.coverage_pct is not None and r.coverage_pct < 100:
            assert r.status is not C.ResultStatus.COMPLETE


def test_provenance_is_present(result):
    p = result.provenance
    assert p.snapshot_hash, "result is not reproducible without a snapshot hash"
    assert p.contract_version == C.CONTRACT_VERSION
    assert p.computed_at


def test_records_reconcile_internally(result):
    for r in result.waterfalls:
        if r.gross_value.amount is None:
            continue
        assert abs(
            (r.gross_value.amount - r.on_invoice_discount.amount) - r.invoice_price.amount
        ) < 0.01
        assert abs(
            (r.invoice_price.amount - r.off_invoice_leakage.amount) - r.pocket_price.amount
        ) < 0.01


# --------------------------------------------------------------------------
# Currency safety
# --------------------------------------------------------------------------
def test_cross_currency_aggregate_is_never_published(w):
    """
    The graph holds five currencies and no FX rate. An aggregate spanning them
    is a number with no unit, so it must abstain -- amounts suppressed, ratios
    suppressed, and the reason stated.
    """
    mixed = result_builder.build(w, grains=("segment",), min_transactions=3)
    assert mixed.waterfalls
    for r in mixed.waterfalls:
        assert r.status is C.ResultStatus.ABSTAINED
        assert r.pocket_price.amount is None
        assert r.pocket_price.currency == "MIXED"
        assert "no FX rate" in (r.pocket_price.note or "")
        assert r.pocket_margin_pct is None, "ratio published from a mixed-currency sum"
        assert r.confidence.band is C.ConfidenceBand.NONE
    assert not C.validate(mixed), "abstaining correctly must not itself be a violation"


def test_single_currency_grain_is_publishable(result):
    """Splitting by currency makes every record a real number in a stated unit."""
    published = [r for r in result.waterfalls if r.status is not C.ResultStatus.ABSTAINED]
    assert published, "nothing publishable even with currency in the grain"
    for r in published:
        assert r.pocket_price.currency != "MIXED"
        assert r.pocket_price.amount is not None
        assert r.key.get("currency") == r.pocket_price.currency


# --------------------------------------------------------------------------
# Serialisation -- this is what actually crosses the boundary
# --------------------------------------------------------------------------
def test_round_trips_to_json(result):
    payload = json.loads(result.to_json())
    assert payload["provenance"]["contract_version"] == C.CONTRACT_VERSION
    assert len(payload["waterfalls"]) == len(result.waterfalls)

    first = payload["waterfalls"][0]
    # enums must serialise as their string values, not as "ResultStatus.COMPLETE"
    assert first["status"] in {s.value for s in C.ResultStatus}
    assert first["confidence"]["band"] in {b.value for b in C.ConfidenceBand}
    # missing money must be visibly missing in JSON too
    assert "amount" in first["list_value"] and "status" in first["list_value"]


def test_contract_validate_catches_a_fabricated_result():
    """The validator must actually reject bad results, not just pass good ones."""
    bad = C.EngineResult(
        provenance=C.Provenance("csv", "/nowhere", snapshot_hash=None),
        waterfalls=(
            C.WaterfallRecord(
                grain="segment",
                key={"segment": "Enterprise"},
                list_value=C.Money(100.0),
                negotiated_off_list=C.Money(0.0),
                gross_value=C.Money(100.0),
                on_invoice_discount=C.Money(10.0),
                invoice_price=C.Money(999.0),   # does not reconcile
                off_invoice_leakage=C.Money(0.0),
                pocket_price=C.Money(90.0),
                cogs=C.Money(50.0),
                pocket_margin=C.Money(40.0),
                pocket_margin_pct=44.4,
                price_realisation_pct=90.0,
                status=C.ResultStatus.COMPLETE,
                confidence=C.Confidence.from_score(0.9),
                evidence=C.Evidence(),          # cites nothing
            ),
        ),
    )
    problems = C.validate(bad)
    assert any("snapshot_hash" in p for p in problems)
    assert any("cites no evidence" in p for p in problems)
    assert any("does not reconcile" in p for p in problems)
