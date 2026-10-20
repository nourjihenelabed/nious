"""The model chain: segmentation -> elasticity, segmentation -> win probability, elasticity
-> optimiser, and how the number of segments is chosen.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from pricing.engine import waterfall as wf
from pricing.graph import CsvGraphSource
from pricing.models import elasticity, optimiser, segmentation, win_probability

V2_EXPORT = Path(__file__).resolve().parents[3] / "axiom_pricing_v2_graph" / "graph"
needs_v2 = pytest.mark.skipif(not V2_EXPORT.exists(), reason="v2 graph export not present")


@pytest.fixture(scope="module")
def w() -> wf.Waterfall:
    """The waterfall over the synthetic snapshot."""
    return wf.compute(CsvGraphSource())


@pytest.fixture(scope="module")
def segments(w) -> segmentation.Segmentation:
    return segmentation.fit(w)


# ---------------------------------------------------------------------------
# choosing k
# ---------------------------------------------------------------------------


def _blobs(k: int, n_per: int = 60, seed: int = 3) -> np.ndarray:
    """Well-separated groups in four dimensions: the answer is k."""
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 6, size=(k, 4))
    return np.vstack([c + rng.normal(0, 0.6, size=(n_per, 4)) for c in centres])


@pytest.mark.parametrize("planted", [3, 5])
def test_choose_k_finds_the_planted_number_of_groups(planted):
    k, method, table, note = segmentation.choose_k(_blobs(planted))
    assert k == planted, note
    assert method in ("kmeans", "hierarchical")
    assert set(table["method"]) == {"kmeans", "hierarchical"}
    assert table["chosen"].sum() == 1
    assert f"k={planted}" in note


def test_the_elbow_is_where_the_curve_bends_most():
    assert segmentation._elbow([2, 3, 4, 5, 6], [100, 40, 30, 25, 22]) == 3


def test_segmentation_reports_how_k_was_chosen(segments):
    assert segments.selection_note
    assert len(segments.selection) > 0
    chosen = segments.selection[segments.selection["chosen"]]
    assert len(chosen) == 1 and int(chosen["k"].iloc[0]) == segments.k
    assert segments.method == chosen["method"].iloc[0]
    assert segments.silhouette is not None and -1 <= segments.silhouette <= 1
    assert segments.method_agreement is not None and -1 <= segments.method_agreement <= 1


def test_a_fixed_k_still_works_and_says_it_was_not_chosen(w):
    fixed = segmentation.fit(w, k=3)
    assert fixed.k == 3
    assert "fixed" in fixed.selection_note
    assert fixed.selection.empty


def test_automatic_k_is_reproducible(w):
    first = segmentation.fit(w).assignments.set_index("customer_id")["segment_label"]
    second = segmentation.fit(w).assignments.set_index("customer_id")["segment_label"]
    pd.testing.assert_series_equal(first.sort_index(), second.sort_index())


# ---------------------------------------------------------------------------
# segmentation -> elasticity
# ---------------------------------------------------------------------------


def _planted_obs(slopes: dict[str, float], n: int = 120, seed: int = 11) -> pd.DataFrame:
    """Centred price/volume points per segment, each with a known slope."""
    rng = np.random.default_rng(seed)
    rows = []
    for label, slope in slopes.items():
        x = rng.normal(0, 0.08, n)
        y = slope * x + rng.normal(0, 0.05, n)
        rows.append(
            pd.DataFrame(
                {
                    elasticity.SEGMENT_SCOPE: label,
                    "log_price": x - x.mean(),
                    "log_quantity": y - y.mean(),
                    "price_source": "recorded unit_price",
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_segment_slopes_are_recovered_and_shrunk_toward_the_book_average():
    obs = _planted_obs({"a": -1.0, "b": -2.0, "c": -3.0})
    model = elasticity._fit_observations(
        obs, [elasticity.SEGMENT_SCOPE], 8, 0.94, "conjugate",
        prior_mean=-2.0, pooling_is_gate=False,
    )
    by = {e.scope[elasticity.SEGMENT_SCOPE]: e for e in model.estimates}
    for label, truth in (("a", -1.0), ("b", -2.0), ("c", -3.0)):
        assert abs(by[label].beta - truth) < 0.25, by[label]
        assert by[label].low < by[label].beta < by[label].high
    # Shrinkage moves the extremes toward the average, never past it.
    assert by["a"].raw_beta > by["a"].beta > -2.0
    assert by["c"].raw_beta < by["c"].beta < -2.0


def test_elasticity_is_fitted_inside_each_segment(w, segments):
    model = elasticity.fit_by_segment(w, segments, backend="conjugate")
    assert model.scope_columns == (elasticity.SEGMENT_SCOPE,)
    labels = set(segments.label_of().values())
    assert {e.scope[elasticity.SEGMENT_SCOPE] for e in model.estimates} <= labels
    for e in model.estimates:
        if e.beta is not None:
            assert e.low is not None and e.high is not None
        assert e.customer_ids, "a segment estimate must cite its customers"
        assert elasticity.SEGMENT_NOTE in e.identification
    assert model.circularity is not None
    assert model.segment_of


def test_segment_observations_remove_each_products_own_level(w, segments):
    obs = elasticity.segment_observations(w, segments.label_of())
    product = w.profile.resolve("product")[0]
    means = obs.groupby([elasticity.SEGMENT_SCOPE, product])[["log_price", "log_quantity"]].mean()
    assert np.allclose(means.to_numpy(), 0.0, atol=1e-9)


def test_segment_estimates_reach_the_contract_with_intervals(w, segments):
    model = elasticity.fit_by_segment(w, segments, backend="conjugate")
    for record in model.to_contract():
        assert elasticity.SEGMENT_SCOPE in record.scope
        if record.beta is not None:
            assert record.credible_interval is not None
        assert record.evidence.customer_ids
        assert record.confidence.reasons


def test_segments_made_only_from_price_behaviour_are_not_trusted(w):
    """With no price-blind grouping to compare against, circularity cannot be ruled out."""
    price_only = segmentation.fit(w, k=3, features_used=elasticity.PRICE_BEHAVIOUR_FEATURES)
    model = elasticity.fit_by_segment(w, price_only, backend="conjugate")
    assert model.circularity.flagged
    assert not any(e.fit_for_optimisation for e in model.estimates)
    assert all("circularity check failed" in " ".join(e.reasons) for e in model.estimates)


def test_segments_made_without_price_behaviour_pass_circularity(w):
    blind = tuple(f for f in segmentation.FEATURES if f not in elasticity.PRICE_BEHAVIOUR_FEATURES)
    check = elasticity.circularity_check(w, segmentation.fit(w, k=3, features_used=blind))
    assert not check.flagged
    assert check.price_features_used == ()


def test_the_circularity_ratio_flags_spread_made_by_the_segmentation(monkeypatch, w, segments):
    """Price-built segments that differ far more than price-blind ones are flagged."""
    spreads = iter([0.9, 0.2])           # with price features, then without
    monkeypatch.setattr(elasticity, "_spread", lambda _model, _n: next(spreads))
    check = elasticity.circularity_check(w, segments)
    assert check.flagged and check.ratio == pytest.approx(4.5)
    assert "4.5 times" in check.note


# ---------------------------------------------------------------------------
# segmentation -> win probability
# ---------------------------------------------------------------------------

#: Planted slopes: price-takers barely react, negotiators react strongly.
SEGMENT_SLOPES = {"price-taking": -3.0, "negotiated": -14.0}


def _segmented_deals(n: int = 2400, seed: int = 5):
    """Decided offers whose price sensitivity depends on the customer's segment."""
    rng = np.random.default_rng(seed)
    customers = [f"C{i:03d}" for i in range(60)]
    segment_of = {c: ("price-taking" if i % 2 else "negotiated") for i, c in enumerate(customers)}
    segment_of.pop("C059")                       # one customer the segmentation abstained on
    customer = rng.choice(customers, n)
    position = rng.uniform(-0.15, 0.15, n)
    slope = np.array([SEGMENT_SLOPES.get(segment_of.get(c, "negotiated")) for c in customer])
    logit = -0.3 + slope * position
    won = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    frame = pd.DataFrame(
        {
            "deal_id": [f"D{i:05d}" for i in range(n)],
            "quote_id": [f"Q{i:05d}" for i in range(n)],
            "customer_id": customer,
            "decided_at": pd.date_range("2024-01-01", periods=n, freq="6h"),
            "won": won,
            "price_position": position,
            "round_number": rng.integers(1, 4, n).astype(float),
            "customer_type": rng.choice(["direct", "tender"], n),
        }
    )
    data = win_probability.WinData(
        frame=frame, numeric=("round_number",), categorical=("customer_type",),
        position_meaning="test offset",
    )
    return data, segment_of


@pytest.fixture(scope="module")
def segmented():
    data, segment_of = _segmented_deals()
    with_segments = win_probability.attach_segments(data, segment_of)
    return with_segments, win_probability.fit(with_segments)


def test_the_segment_and_price_x_segment_are_inputs(segmented):
    data, model = segmented
    assert win_probability.SEGMENT_COLUMN in data.categorical
    assert model.uses_segments
    # One interaction per segment except the baseline, and none for the unsegmented.
    assert len(data.interactions) == 1
    assert not any(win_probability.UNSEGMENTED in c for c in data.interactions)
    assert (data.frame["segment"] == win_probability.UNSEGMENTED).any()


def test_price_sensitivity_differs_by_segment_as_planted(segmented):
    _, model = segmented
    effects = {e["label"]: e for e in model.segment_price_effects.values()}
    steep, flat = effects["negotiated"], effects["price-taking"]
    assert steep["win_change_per_5pct"] < flat["win_change_per_5pct"] < 0
    for e in (steep, flat):
        assert e["win_change_low"] <= e["win_change_per_5pct"] <= e["win_change_high"]


def test_the_segmented_model_still_passes_all_three_checks(segmented):
    _, model = segmented
    assert model.usable, model.problems
    assert model.price_effect["win_change_high"] < 0                  # price effect negative
    assert model.metrics["calibration_error"] <= model.metrics["calibration_error_by_chance"]
    assert model.metrics["brier"] < model.metrics["brier_no_model"]   # beats the base rate
    # And knowing the segment helps on later deals when sensitivity really differs.
    assert model.metrics["log_loss"] < model.metrics["log_loss_without_segment"]


def test_moving_the_price_moves_the_interaction_with_it(segmented):
    """The interaction is rebuilt from the price, so curves fall in every segment."""
    data, model = segmented
    positions = np.linspace(-0.1, 0.1, 9)
    for code in data.frame["segment"].unique():
        context = data.frame[data.frame["segment"] == code].head(20)
        curve = model.curve(context, positions)
        assert np.all(np.diff(curve) < 0), code


def test_the_report_names_each_segment_and_the_timing_caveat(segmented):
    _, model = segmented
    text = model.report()
    assert "Price effect by customer segment" in text
    assert win_probability.SEGMENT_TIMING_NOTE in text


def test_without_segments_the_model_is_unchanged():
    data, _ = _segmented_deals()
    model = win_probability.fit(data)
    assert not model.uses_segments and not model.segment_price_effects


# ---------------------------------------------------------------------------
# elasticity -> optimiser
# ---------------------------------------------------------------------------


def _estimate(label: str, beta: float, low: float, high: float, fit: bool = True):
    return elasticity.Estimate(
        scope={elasticity.SEGMENT_SCOPE: label}, beta=beta, low=low, high=high,
        raw_beta=beta, standard_error=0.1, n_observations=200, log_price_sd=0.1,
        r_squared=0.3, shrinkage=0.2, fit_for_optimisation=fit,
    )


def _elasticity_model(estimates, segment_of):
    return elasticity.ElasticityModel(
        estimates=tuple(estimates), prior_mean=-2.0, prior_sd=0.3,
        scope_columns=(elasticity.SEGMENT_SCOPE,), n_scopes_fitted=len(estimates),
        n_scopes_abstained=0, segment_of=segment_of,
    )


def _book(paid: float = 95.0):
    facts = pd.DataFrame(
        {
            "customer_id": ["C001", "C001"],
            "product_id": ["P1", "P1"],
            "unit_price_recorded": [paid, paid],
            "currency_code": ["SAR", "SAR"],
            "order_date": pd.to_datetime(["2025-01-01", "2025-02-01"]),
            "certified": [True, True],
        }
    )
    return SimpleNamespace(facts=facts, profile=SimpleNamespace(unit_price_column="unit_price_recorded"))


def _refs():
    products = pd.DataFrame(
        {
            "product_id": ["P1"], "list_price": [130.0], "unit_cost": [60.0],
            "reference_price": [100.0], "n_reference_observations": [4],
            "reference_ids": ["Q1;Q2"],
        }
    )
    return win_probability.ReferencePrices(products=products, currency="SAR")


@pytest.fixture(scope="module")
def plain_model():
    data, _ = _segmented_deals()
    return data, win_probability.fit(data)


def test_the_volume_rule_removes_prices_that_lose_too_much_volume(plain_model):
    data, model = plain_model
    steep = _elasticity_model([_estimate("negotiated", -4.0, -4.5, -3.5)], {"C001": "negotiated"})
    free, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1"
    )
    ruled, curve = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1",
        elasticity=steep, min_volume_share=0.8,
    )
    assert ruled.status == "complete", ruled.note
    # Every allowed price keeps 80% of volume at the most elastic end of the interval.
    allowed = curve[curve["allowed"]]
    assert (allowed["volume_share_worst_case"] >= 0.8 - 1e-9).all()
    assert ruled.mid <= free.mid
    assert ruled.volume_share_low <= ruled.volume_share_at_mid <= ruled.volume_share_high
    assert ruled.elasticity_used == pytest.approx(-4.0)
    assert any("volume is expected at" in r for r in ruled.confidence_reasons)


def test_a_binding_volume_rule_is_listed_as_a_constraint(plain_model):
    data, model = plain_model
    steep = _elasticity_model([_estimate("negotiated", -8.0, -9.0, -7.0)], {"C001": "negotiated"})
    # Unconstrained, the best price here is about 91. Paying 85 today, a price
    # above ~85.5 is expected to cost more than a tenth of the volume.
    rec, _ = optimiser.recommend_with_win_model(
        _book(paid=85.0), model, _refs(), data.frame, "C001", "P1",
        elasticity=steep, min_volume_share=0.9,
    )
    assert rec.status == "complete", rec.note
    assert rec.mid <= 85.0 * 0.9 ** (1 / -9.0) + 1e-6
    assert any("expected volume at least 90%" in c for c in rec.constraints)


def test_no_elasticity_fit_for_optimisation_skips_the_rule_and_says_so(plain_model):
    data, model = plain_model
    unfit = _elasticity_model(
        [_estimate("negotiated", -4.0, -4.5, 0.2, fit=False)], {"C001": "negotiated"}
    )
    with_rule_off, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1", elasticity=unfit
    )
    assert with_rule_off.status == "complete"
    skipped = "volume rule not applied: no elasticity estimate passed"
    assert any(skipped in c for c in with_rule_off.constraints)
    assert any(skipped in r for r in with_rule_off.confidence_reasons)
    assert with_rule_off.volume_share_at_mid is None
    record = with_rule_off.to_contract(is_win_rate=True)
    assert any(skipped in c for c in record.constraints_applied)
    assert any(skipped in r for r in record.confidence.reasons)


def test_a_customer_without_a_segment_gets_no_volume_rule(plain_model):
    data, model = plain_model
    other = _elasticity_model([_estimate("negotiated", -4.0, -4.5, -3.5)], {"C999": "negotiated"})
    rec, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1", elasticity=other
    )
    assert any("this customer has no segment" in r for r in rec.confidence_reasons)


def test_skipping_the_volume_rule_lowers_confidence(plain_model):
    data, model = plain_model
    good = _elasticity_model([_estimate("negotiated", -0.5, -0.6, -0.4)], {"C001": "negotiated"})
    with_rule, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1", elasticity=good
    )
    without, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), data.frame, "C001", "P1", elasticity=None
    )
    assert without.confidence_score < with_rule.confidence_score


def test_volume_rule_uses_the_most_elastic_end_of_the_interval():
    rule = optimiser.VolumeRule(beta=-2.0, low=-3.0, high=-1.0, scope="segment x")
    prices = np.array([100.0, 110.0])
    worst = rule.share(prices, 100.0, "low")
    central = rule.share(prices, 100.0, "mid")
    assert worst[0] == pytest.approx(1.0) and worst[1] < central[1]


# ---------------------------------------------------------------------------
# the whole chain on the v2 export
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def v2_run():
    from pricing.engine import pipeline
    from pricing.graph.json_source import GraphJsonSource
    from pricing.profiles import get_profile

    profile = get_profile("axiom_v2")
    src = GraphJsonSource(V2_EXPORT, profile=profile)
    return pipeline.run(src, profile=profile, with_models=True, track=False)


@needs_v2
@pytest.mark.slow
def test_v2_every_link_is_live(v2_run):
    models = v2_run.models
    links = models["links"]
    assert set(links) == {
        "segmentation -> elasticity",
        "segmentation -> win probability",
        "elasticity -> optimiser",
    }
    assert not any(text.startswith("not linked") for text in links.values()), links
    assert models["elasticity"].scope_columns == (elasticity.SEGMENT_SCOPE,)
    assert models["acceptance"].uses_segments and models["acceptance"].usable
    assert v2_run.ok, v2_run.contract_violations


@needs_v2
@pytest.mark.slow
def test_v2_contract_carries_the_chain(v2_run):
    result = v2_run.result
    assert result.segments and result.elasticities and result.recommendations
    assert all(elasticity.SEGMENT_SCOPE in e.scope for e in result.elasticities)
    priced = [r for r in result.recommendations if r.status.value == "complete"]
    assert priced
    assert all(
        any("volume is expected at" in reason for reason in r.confidence.reasons)
        or any("volume rule not applied" in c for c in r.constraints_applied)
        for r in priced
    )
