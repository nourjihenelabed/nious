"""Week 4-6 tests: readiness, margin analytics, segmentation, elasticity, optimisation."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.analytics import margin  # noqa: E402
from pricing.contracts import schema as C  # noqa: E402
from pricing.engine import pipeline  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource  # noqa: E402
from pricing.models import acceptance as acceptance_model  # noqa: E402
from pricing.models import elasticity, optimiser, segmentation  # noqa: E402
from pricing.quality import readiness  # noqa: E402


@pytest.fixture(scope="module")
def src() -> CsvGraphSource:
    """The synthetic snapshot the module runs against."""
    return CsvGraphSource()


@pytest.fixture(scope="module")
def w(src) -> wf.Waterfall:
    """The waterfall over the whole snapshot."""
    return wf.compute(src)


# ---------------------------------------------------------------------------
# D3 -- readiness
# ---------------------------------------------------------------------------


def test_readiness_scores_the_snapshot_and_names_what_is_missing(src):
    """A score with no shopping list attached is not actionable."""
    verdict = readiness.assess(src)
    assert 0 <= verdict.score <= 100
    assert verdict.missing_fields, "a snapshot with known gaps must name them"
    assert not verdict.blocking


def test_readiness_penalises_document_grain_steps(src):
    """The grain check must actually notice that half the chain cannot be sliced."""
    verdict = readiness.assess(src)
    grain = next(c for c in verdict.checks if c.name == "grain")
    assert grain.score < 100
    # Named the way the report shows it: "on-invoice discounts", not the column.
    assert any("on-invoice discounts" in m for m in grain.missing)


def test_a_blocking_finding_forces_blocked_whatever_the_score(src, tmp_path):
    """One dangling reference must not be outweighed by good field coverage."""
    from pricing import config

    for fname in config.CSV_FILES.values():
        (tmp_path / fname).write_bytes((Path(src.data_dir) / fname).read_bytes())
    lines = pd.read_csv(tmp_path / "transaction_lines.csv")
    lines.loc[0, "product_id"] = "PRD-GHOST"
    lines.to_csv(tmp_path / "transaction_lines.csv", index=False)

    verdict = readiness.assess(CsvGraphSource(tmp_path))
    assert verdict.band is readiness.Band.BLOCKED
    assert verdict.blocking


def test_readiness_refuses_a_source_it_cannot_even_open(tmp_path):
    """Unreadable tables must produce a verdict, not an exception."""
    (tmp_path / "transactions.csv").write_text("not,a,snapshot\n", encoding="utf-8")
    verdict = readiness.assess(CsvGraphSource(tmp_path))
    assert verdict.band is readiness.Band.BLOCKED
    assert verdict.blocking


def test_capabilities_cover_every_deliverable(src):
    """The admission test has to answer for each thing the module claims to do."""
    verdict = readiness.assess(src)
    assert set(verdict.capabilities["deliverable"]) == set(readiness.DEPTH_REQUIREMENTS)
    for _, row in verdict.capabilities.iterrows():
        assert row["status"] in ("READY", "LIMITED", "BLOCKED")
        if row["status"] != "READY":
            assert row["reason"], f"{row['deliverable']} is not READY and says nothing"


# ---------------------------------------------------------------------------
# D3 -- margin analytics
# ---------------------------------------------------------------------------


def test_the_leakage_bridge_accounts_for_all_of_the_leakage(w):
    """Every concession between list and pocket, adding to the whole gap."""
    bridge = margin.leakage_bridge(w)
    total = w.totals["list_value"] - w.totals["pocket_price"]
    assert abs(bridge["value"].sum() - total) < 0.01
    # The shares are rounded to two places for reporting, so they sum to 100 only
    # up to that rounding -- one cent per bar, not a missing bar.
    assert abs(bridge["share_of_leakage"].sum() - 100) <= 0.005 * len(bridge)


def test_the_leakage_bridge_abstains_at_product_grain(w):
    """A bridge missing one of its bars is worse than no bridge."""
    by_product = margin.leakage_bridge(w, "product")
    assert not by_product["complete"].any()
    assert by_product["note"].iloc[0]
    assert by_product["on_invoice_discount"].isna().all()


def test_price_band_uses_a_realised_price_not_list(w):
    """Banding list price gives a spread of zero and calls it a finding."""
    bands = margin.price_band(w)
    complete = bands[bands["status"] == "complete"]
    assert len(complete)
    assert (complete["spread_pct"] > 0).any(), "no product shows any price dispersion"


def test_price_band_abstains_on_thin_products(w):
    """A spread over one or two deals is a shape, not a band."""
    bands = margin.price_band(w, min_documents=1000)
    assert (bands["status"] == "abstained").all()
    assert bands["spread_pct"].isna().all()


def test_margin_at_risk_prices_the_gap_to_the_floor(w):
    """The uplift must actually reach the floor, or the prize is wrong."""
    floor = 10.0
    at_risk = margin.margin_at_risk(w, floor_pct=floor)
    assert len(at_risk)
    assert (at_risk["margin_pct"] < floor).all()

    lifted = at_risk["required_price"]
    cost = at_risk["pocket_price"] - at_risk["pocket_margin"]
    assert ((100 * (lifted - cost) / lifted) - floor).abs().max() < 1e-6


def test_margin_quadrants_split_at_the_books_own_medians(w):
    """A fixed threshold imports an assumption from someone else's business."""
    q = margin.margin_quadrants(w)
    assert set(q["quadrant"]) <= {
        "large & profitable", "large & thin -- negotiate",
        "small & profitable -- grow", "small & thin -- serve differently",
    }
    assert q["quadrant"].nunique() > 1


# ---------------------------------------------------------------------------
# D4 -- segmentation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def segments(w) -> segmentation.Segmentation:
    """One fitted segmentation over the snapshot."""
    return segmentation.fit(w)


def test_segmentation_is_reproducible(w):
    """Same snapshot, same seed, same answer -- or it cannot be governed."""
    first = segmentation.fit(w).assignments.set_index("customer_id")["segment_label"]
    second = segmentation.fit(w).assignments.set_index("customer_id")["segment_label"]
    pd.testing.assert_series_equal(first.sort_index(), second.sort_index())


def test_every_customer_appears_exactly_once(w, segments):
    """A customer who vanishes is indistinguishable from one who was never there."""
    customers = set(w.keys["customer_id"].dropna())
    assert set(segments.assignments["customer_id"]) == customers
    assert not segments.assignments["customer_id"].duplicated().any()


def test_thin_customers_abstain_rather_than_being_labelled(w):
    """Clustering labels everyone; that is the failure mode, not the feature."""
    strict = segmentation.fit(w, min_deals=12)
    abstained = strict.assignments[strict.assignments["status"] == "abstained"]
    assert len(abstained), "expected some customers below a 12-deal threshold"
    assert (abstained["segment_label"] == "unsegmented").all()
    assert abstained["segment_score"].isna().all()
    assert abstained["note"].str.len().gt(0).all()


def test_segment_labels_are_economic_not_ordinal(segments):
    """`cluster_2` is not a finding."""
    labels = set(segments.assignments["segment_label"]) - {"unsegmented"}
    assert len(labels) == segments.k
    assert not any(label.startswith("cluster") for label in labels)


def test_segmentation_refuses_more_clusters_than_customers(w):
    """Better to refuse than to return k singleton clusters as insight."""
    with pytest.raises(ValueError, match="cannot support"):
        segmentation.fit(w, k=999)


def test_segment_records_satisfy_the_contract(w, segments):
    """Low confidence with no reason is exactly what the contract forbids."""
    records = segments.to_contract(w)
    assert len(records) == len(segments.assignments)
    for record in records:
        if record.confidence.band in (C.ConfidenceBand.LOW, C.ConfidenceBand.NONE):
            assert record.confidence.reasons
        if record.status is C.ResultStatus.ABSTAINED:
            assert record.segment_score is None


def test_segment_shares_of_value_add_up(segments):
    """A profile table whose shares do not sum to 100 has lost a segment."""
    table = segments.profile_table()
    assert abs(table["share_of_value_pct"].sum() - 100) < 0.05


# ---------------------------------------------------------------------------
# D5 -- elasticity
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def elasticities(w) -> elasticity.ElasticityModel:
    """One fitted elasticity model over the snapshot."""
    return elasticity.fit(w)


def test_elasticity_prefers_a_recorded_price_over_a_derived_one(w, elasticities):
    """Deriving price as value/quantity manufactures a negative slope."""
    assert elasticities.price_source.startswith("recorded")
    obs = elasticity.observations(w)
    assert (obs["price"] > 0).all()


def test_no_point_estimate_is_reported_without_an_interval(elasticities):
    """The contract refuses it, and this module must not be able to produce it."""
    for e in elasticities.estimates:
        assert (e.beta is None) == (e.interval is None)
    for record in elasticities.to_contract():
        assert (record.beta is None) == (record.credible_interval is None)
    assert not C.validate(
        C.EngineResult(
            provenance=C.Provenance("csv", "/x", "hash"),
            elasticities=elasticities.to_contract(),
        )
    )


def test_thin_scopes_are_shrunk_toward_the_pooled_prior(elasticities):
    """A scope with little to say must end up nearer the prior than its own slope."""
    shrunk = [e for e in elasticities.estimates if e.beta is not None and e.shrinkage > 0.5]
    assert shrunk, "expected some heavily shrunk scopes on this snapshot"
    for e in shrunk:
        to_prior = abs(e.beta - elasticities.prior_mean)
        to_own = abs(e.beta - e.raw_beta)
        assert to_prior < to_own, f"{e.scope} was not actually pulled toward the prior"


def test_an_interval_crossing_zero_is_not_fit_for_optimisation(elasticities):
    """Demand that may not respond to price at all cannot drive a price."""
    for e in elasticities.estimates:
        if e.interval and e.interval[1] >= 0:
            assert not e.fit_for_optimisation


def test_a_scope_with_no_price_variation_abstains(w):
    """No experiment happened, so there is nothing to learn."""
    model = elasticity.fit(w, min_observations=1)
    flat = [e for e in model.estimates if e.log_price_sd < elasticity.MIN_LOG_PRICE_SD]
    for e in flat:
        assert e.beta is None
        assert any("price variation" in r or "barely moved" in r for r in e.reasons)


def test_every_estimate_records_the_identification_problem(elasticities):
    """Observational prices cannot be fixed here, so they must at least be stated."""
    for e in elasticities.estimates:
        assert "observational" in e.identification
    for record in elasticities.to_contract():
        assert any("observational" in r for r in record.confidence.reasons)


# ---------------------------------------------------------------------------
# D6 -- acceptance and optimisation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def acceptance(w) -> acceptance_model.AcceptanceModel:
    """One fitted acceptance model over the snapshot."""
    return acceptance_model.fit(w)


def test_acceptance_is_labelled_as_a_benchmark_not_a_win_rate(acceptance):
    """This source has no losses, so no win rate is estimable from it."""
    assert acceptance.mode == "empirical_acceptance"
    assert acceptance.is_win_rate is False
    assert "not an observed win rate" in acceptance.interpretation


def test_acceptance_refuses_to_score_its_own_calibration(acceptance):
    """Scoring against won deals alone would return a meaningless 100%."""
    table = acceptance.calibration()
    assert table["observed"].isna().all()
    assert "observed outcomes" in table["note"].iloc[0]


def test_acceptance_is_monotone_in_discount_depth(acceptance):
    """A deeper discount can never be less likely to be accepted."""
    scope = dict(zip(acceptance.scope_columns, next(iter(acceptance.curves)), strict=True))
    depths = np.linspace(-0.1, 0.6, 40)
    curve = acceptance.curve(scope, depths)
    assert np.all(np.diff(curve) >= -1e-12)


def test_acceptance_never_reports_certainty(acceptance):
    """Laplace smoothing exists so no price comes back as certainly accepted."""
    scope = dict(zip(acceptance.scope_columns, next(iter(acceptance.curves)), strict=True))
    assert acceptance.probability(scope, -10.0).probability > 0
    assert acceptance.probability(scope, 10.0).probability < 1


def test_acceptance_abstains_on_an_unknown_scope(acceptance):
    """An unfitted scope must say so rather than return a number."""
    prediction = acceptance.probability(
        dict.fromkeys(acceptance.scope_columns, "PRD-NOT-A-PRODUCT"), 0.1
    )
    assert prediction.status == "abstained"
    assert math.isnan(prediction.probability)
    assert prediction.note


def test_a_recommendation_never_prices_below_cost(w, acceptance):
    """The floor is a constraint on the grid, not a clip applied afterwards."""
    recs = optimiser.recommend_book(w, acceptance, top=40)
    complete = recs[recs["status"] == "complete"]
    assert len(complete)
    floor = complete["unit_cost"] / (1 - optimiser.MARGIN_FLOOR)
    assert (complete["low"] >= floor - 1e-6).all()
    assert (complete["high"] <= complete["list_price"] + 1e-6).all()


def test_a_recommendation_is_a_band_and_the_band_is_ordered(w, acceptance):
    """`PriceRange` rejects an unordered band at construction; so must the optimiser."""
    recs = optimiser.recommend_book(w, acceptance, top=20)
    complete = recs[recs["status"] == "complete"]
    assert (complete["low"] <= complete["mid"]).all()
    assert (complete["mid"] <= complete["high"]).all()


def test_every_recommendation_lists_the_constraints_that_bound_it(w, acceptance):
    """A recommendation nobody may act on is not a recommendation."""
    recs = optimiser.recommend_book(w, acceptance, top=20)
    for _, row in recs[recs["status"] == "complete"].iterrows():
        assert row["constraints"], f"{row['customer_id']}/{row['product_id']} names no constraints"


def test_a_pair_with_no_comparable_deals_abstains(w, acceptance):
    """Most pairs in a real book have traded twice; that must produce nothing."""
    rec = optimiser.recommend(w, acceptance, "CUS-001", "PRD-NOT-A-PRODUCT")
    assert rec.status == "abstained"
    assert rec.mid is None
    assert "comparable" in rec.note

    record = rec.to_contract()
    assert record.status is C.ResultStatus.ABSTAINED
    assert record.price_range is None
    assert record.confidence.reasons


def test_a_proxy_acceptance_can_never_be_reported_as_high_confidence(w, acceptance):
    """However many deals stand behind a proxy, it is still a proxy."""
    recs = optimiser.recommend_book(w, acceptance, top=30)
    for _, row in recs[recs["status"] == "complete"].iterrows():
        record = optimiser.Recommendation(
            **{k: v for k, v in row.items() if k != "constraints"},
            constraints=tuple(c for c in str(row["constraints"]).split("; ") if c),
        ).to_contract("EUR", is_win_rate=acceptance.is_win_rate)
        assert record.confidence.band is not C.ConfidenceBand.HIGH


def test_the_recommended_price_really_does_maximise_expected_profit(w, acceptance):
    """The one arithmetic claim in the module, checked by brute force."""
    rec = optimiser.recommend_book(w, acceptance, top=5)
    row = rec[rec["status"] == "complete"].iloc[0]
    scope = dict.fromkeys(acceptance.scope_columns, row["product_id"])

    grid = np.linspace(row["low"], row["high"], 200)
    expected = acceptance.curve(scope, 1 - grid / row["list_price"]) * (grid - row["unit_cost"])
    assert expected.max() <= row["expected_unit_margin"] * (1 + optimiser.TOLERANCE_OF_MAX) + 1e-6


# ---------------------------------------------------------------------------
# the whole run
# ---------------------------------------------------------------------------


def test_the_pipeline_carries_every_deliverable_into_one_contract(src):
    """Track B receives one object, and it must still validate with models attached."""
    run = pipeline.run(src, track=False, with_models=True, top_recommendations=20)
    assert run.ok
    assert not run.contract_violations
    assert run.readiness is not None
    assert run.margin["summary"] is not None

    result = run.result
    assert result.waterfalls and result.segments and result.elasticities
    assert result.recommendations
    assert not C.validate(result)

    import json

    json.loads(result.to_json())


def test_the_pipeline_skips_what_readiness_says_is_impossible(src):
    """A blocked deliverable is skipped with a reason, not attempted and caught."""
    verdict = readiness.assess(src)
    verdict.capabilities.loc[
        verdict.capabilities["deliverable"] == "segmentation", "status"
    ] = "BLOCKED"
    verdict.capabilities.loc[
        verdict.capabilities["deliverable"] == "segmentation", "reason"
    ] = "blocked for this test"

    models = pipeline.fit_models(wf.compute(src), verdict, top_recommendations=5)
    assert "segmentation" not in models
    assert models["skipped"]["segmentation"] == "blocked for this test"
