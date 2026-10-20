"""D6 and D7: the win-probability model, the optimiser on top of it, monitoring, the model
registry, and the export reader they all start from.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from pricing.graph.json_source import NotAGraphExport, read_export
from pricing.models import optimiser, win_probability
from pricing.monitoring import monitor, psi

V2_EXPORT = Path(__file__).resolve().parents[3] / "axiom_pricing_v2_graph" / "graph"
needs_v2 = pytest.mark.skipif(not V2_EXPORT.exists(), reason="v2 graph export not present")

#: The planted effect: each +1% on price lowers the log-odds of winning by 0.08.
TRUE_SLOPE = -8.0


def synthetic_deals(n: int = 1600, slope: float = TRUE_SLOPE, seed: int = 7) -> win_probability.WinData:
    """Decided offers whose chance of winning follows a known logistic curve."""
    rng = np.random.default_rng(seed)
    position = rng.uniform(-0.15, 0.15, n)
    kind = rng.choice(["direct", "tender"], n)
    rounds = rng.integers(1, 4, n)
    logit = -0.5 + slope * position + 0.6 * (kind == "direct") + 0.1 * rounds
    won = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    frame = pd.DataFrame(
        {
            "deal_id": [f"D{i:05d}" for i in range(n)],
            "quote_id": [f"Q{i:05d}" for i in range(n)],
            "customer_id": [f"C{i % 40:03d}" for i in range(n)],
            "decided_at": pd.date_range("2024-01-01", periods=n, freq="8h"),
            "won": won,
            "price_position": position,
            "round_number": rounds.astype(float),
            "customer_type": kind,
        }
    )
    return win_probability.WinData(
        frame=frame, numeric=("round_number",), categorical=("customer_type",),
        position_meaning="test offset",
    )


@pytest.fixture(scope="module")
def deals():
    return synthetic_deals()


@pytest.fixture(scope="module")
def model(deals):
    return win_probability.fit(deals)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------


def test_the_model_recovers_a_planted_price_effect(model, deals):
    """Higher prices must lose more deals, by about as much as was planted."""
    assert model.usable, model.problems
    # The planted slope, turned into "points of win rate per +5%" on these deals.
    rows = deals.priced
    logit = -0.5 + TRUE_SLOPE * rows["price_position"] + 0.6 * (rows["customer_type"] == "direct")
    logit = logit + 0.1 * rows["round_number"]
    p0 = 1 / (1 + np.exp(-logit))
    p1 = 1 / (1 + np.exp(-(logit + TRUE_SLOPE * 0.05)))
    planted = 100 * float((p1 - p0).mean())
    effect = model.price_effect
    assert effect["win_change_high"] < 0
    assert effect["win_change_low"] - 1.5 <= planted <= effect["win_change_high"] + 1.5


def test_a_well_specified_model_passes_calibration(model):
    """On deals drawn from the curve it learned, the gap is within chance."""
    m = model.metrics
    assert m["calibration_error"] <= m["calibration_error_by_chance"]
    assert m["brier"] < m["brier_no_model"]


def test_win_probability_falls_as_price_rises(model, deals):
    positions = np.linspace(-0.15, 0.15, 13)
    curve = model.curve(deals.priced.head(50), positions)
    assert np.all(np.diff(curve) < 0)


def test_a_model_that_sees_no_price_effect_is_not_used_for_prices():
    """No evidence that price matters means no optimum to find."""
    flat = win_probability.fit(synthetic_deals(slope=0.0, seed=11))
    assert not flat.usable
    assert any("higher prices" in p for p in flat.problems)


def test_too_few_priced_offers_is_refused_in_words():
    small = synthetic_deals(n=120)
    with pytest.raises(ValueError, match="decided quotes have a recorded price position"):
        win_probability.fit(small)


def test_offers_without_a_price_position_are_left_out_of_training(deals):
    frame = deals.frame.copy()
    frame.loc[frame.index[:300], "price_position"] = np.nan
    partial = win_probability.WinData(frame, deals.numeric, deals.categorical)
    fitted = win_probability.fit(partial)
    assert fitted.n_train + fitted.n_test == len(frame) - 300


def test_calibration_error_of_a_perfect_model_is_within_chance():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.05, 0.95, 400)
    y = (rng.random(400) < p).astype(int)
    assert win_probability.calibration_error(y, p) <= win_probability.chance_calibration_error(p)


def test_calibration_error_catches_a_model_that_is_badly_off():
    rng = np.random.default_rng(4)
    p = rng.uniform(0.05, 0.95, 400)
    y = (rng.random(400) < np.clip(p + 0.25, 0, 1)).astype(int)
    assert win_probability.calibration_error(y, p) > win_probability.chance_calibration_error(p)


# ---------------------------------------------------------------------------
# the optimiser
# ---------------------------------------------------------------------------


def _book(customer: str = "C001", product: str = "P1", paid: float = 95.0):
    """A minimal waterfall: one customer-product pair that has traded twice."""
    facts = pd.DataFrame(
        {
            "customer_id": [customer, customer],
            "product_id": [product, product],
            "unit_price_recorded": [paid, paid],
            "currency_code": ["SAR", "SAR"],
            "order_date": pd.to_datetime(["2025-01-01", "2025-02-01"]),
            "certified": [True, True],
        }
    )
    return SimpleNamespace(facts=facts, profile=SimpleNamespace(unit_price_column="unit_price_recorded"))


def _refs(reference=100.0, cost=60.0, list_price=110.0, n=4):
    products = pd.DataFrame(
        {
            "product_id": ["P1"],
            "list_price": [list_price],
            "unit_cost": [cost],
            "reference_price": [reference],
            "n_reference_observations": [n],
            "reference_ids": ["Q1;Q2"],
        }
    )
    return win_probability.ReferencePrices(products=products, currency="SAR")


def test_the_recommendation_respects_every_rule_and_maximises_expected_profit(model, deals):
    rec, curve = optimiser.recommend_with_win_model(
        _book(), model, _refs(), deals.frame, "C001", "P1",
        margin_floor=0.05, min_win_probability=0.25,
    )
    assert rec.status == "complete", rec.note
    assert rec.low <= rec.mid <= rec.high
    assert rec.low >= 60.0 / 0.95 - 1e-9          # cost plus 5%
    assert rec.high <= 110.0 + 1e-9               # list price
    assert rec.acceptance >= 0.25                 # volume rule
    # The curve is saved rounded to cents, so neighbouring prices can tie; the
    # claim is that no allowed price earns more than the recommended one.
    allowed = curve[curve["allowed"]]
    assert rec.expected_unit_margin == pytest.approx(
        allowed["expected_profit_per_unit"].max(), abs=0.01
    )
    assert rec.evidence_ids and rec.confidence_reasons


def test_expected_profit_is_single_peaked(model, deals):
    _, curve = optimiser.recommend_with_win_model(
        _book(), model, _refs(), deals.frame, "C001", "P1"
    )
    assert optimiser._local_peaks(curve["expected_profit_per_unit"].to_numpy()) == 1


def test_a_product_with_no_observed_reference_is_not_priced(model, deals):
    rec, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(reference=np.nan), deals.frame, "C001", "P1"
    )
    assert rec.status == "abstained"
    assert "reference price is unknown" in rec.note


def test_rules_that_cannot_all_be_met_produce_no_price(model, deals):
    """Cost above list: no price clears the floor and stays under the ceiling."""
    rec, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(cost=120.0), deals.frame, "C001", "P1"
    )
    assert rec.status == "abstained"
    assert "no price meets every rule" in rec.note


def test_confidence_drops_when_the_evidence_is_thin(model, deals):
    full, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(n=4), deals.frame, "C001", "P1"
    )
    thin, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(n=1), deals.frame, "C001", "P1"
    )
    assert thin.confidence_score < full.confidence_score
    assert any("observed only 1" in r for r in thin.confidence_reasons)


def test_a_win_model_recommendation_can_be_reported_as_high_confidence(model, deals):
    """Unlike the won-deals-only benchmark, real win/loss history is not capped."""
    from pricing.contracts import schema as C

    rec, _ = optimiser.recommend_with_win_model(
        _book(), model, _refs(), deals.frame, "C001", "P1"
    )
    record = rec.to_contract(is_win_rate=True)
    assert record.status is C.ResultStatus.COMPLETE
    assert record.evidence.transaction_ids
    assert record.confidence.score == pytest.approx(rec.confidence_score)


# ---------------------------------------------------------------------------
# monitoring
# ---------------------------------------------------------------------------


def test_psi_is_zero_for_the_same_deals_and_large_for_shifted_ones():
    rng = np.random.default_rng(5)
    a = pd.Series(rng.normal(0, 1, 2000))
    assert psi(a, a) == pytest.approx(0.0, abs=1e-9)
    assert psi(a, pd.Series(rng.normal(1.5, 1, 2000))) > 0.25


def test_monitoring_flags_a_model_whose_win_rate_has_drifted(model, deals):
    recent = deals.priced.tail(400).copy()
    recent["decided_at"] = pd.date_range("2026-01-01", periods=400, freq="12h")
    columns = [*model.numeric, *model.categorical]

    def predict(frame):
        return model.predict(frame[columns])

    steady = monitor(predict, deals.priced, recent, tuple(columns), "2025-12-31")
    assert not steady.needs_refresh, steady.refresh

    drifted = recent.copy()
    drifted["won"] = (np.random.default_rng(9).random(len(drifted)) < 0.85).astype(int)
    report = monitor(predict, deals.priced, drifted, tuple(columns), "2025-12-31")
    assert report.needs_refresh
    assert "were won" in " ".join(report.refresh)


def test_monitoring_with_nothing_new_says_so(deals):
    report = monitor(lambda f: np.zeros(len(f)), deals.priced, deals.priced.iloc[:0],
                     ("round_number",), "2026-06-30")
    assert not report.needs_refresh
    assert "nothing to compare yet" in report.text()


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_register_promote_and_roll_back(tmp_path, monkeypatch, model, deals):
    mlflow = pytest.importorskip("mlflow")
    from pricing.tracking import registry

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setattr(registry, "TRACKING_URI", uri)
    mlflow.set_tracking_uri(uri)
    experiment = mlflow.create_experiment("t", artifact_location=(tmp_path / "a").as_uri())

    versions = []
    for _ in range(2):
        with mlflow.start_run(experiment_id=experiment):
            versions.append(registry.register(model, "synthetic", deals))
    assert versions == ["1", "2"]

    first = registry.promote("synthetic", version="1")
    assert first.promoted
    second = registry.promote("synthetic")          # newest candidate, as good as v1
    assert second.promoted and second.version == "2"
    assert "previous_champion" in second.message

    back = registry.rollback("synthetic")
    assert back.promoted and back.version == "1"
    table = registry.status("synthetic").set_index("version")
    assert "champion" in table.loc[1, "aliases"].split(", ")

    version, pipeline, tags = registry.load("synthetic")
    assert version == "1" and tags["usable"] == "True"
    p = pipeline.predict_proba(
        win_probability._prepare(deals.priced[[*model.numeric, *model.categorical]].head(3),
                                 model.categorical)
    )[:, 1]
    assert np.allclose(p, model.predict(deals.priced.head(3)))


def test_a_model_that_failed_its_checks_is_not_promoted(tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    from pricing.tracking import registry

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setattr(registry, "TRACKING_URI", uri)
    mlflow.set_tracking_uri(uri)
    experiment = mlflow.create_experiment("t", artifact_location=(tmp_path / "a").as_uri())
    flat_deals = synthetic_deals(slope=0.0, seed=11)
    flat = win_probability.fit(flat_deals)
    with mlflow.start_run(experiment_id=experiment):
        registry.register(flat, "synthetic", flat_deals)
    decision = registry.promote("synthetic")
    assert not decision.promoted
    assert "failed its own checks" in decision.message


# ---------------------------------------------------------------------------
# reading any of the export layouts
# ---------------------------------------------------------------------------

NODES = [
    {"id": "n1", "labels": ["Order"], "properties": {"order_id": "O1"}},
    {"id": "n2", "labels": ["OrderLine"], "properties": {"order_id": "O1"}},
]
RELS = [{"source": "n2", "target": "n1", "type": "PART_OF", "properties": {}}]


def _ndjson(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_the_reader_takes_one_document_one_ndjson_file_or_a_folder(tmp_path):
    single = tmp_path / "export.json"
    single.write_text(json.dumps({"nodes": NODES, "relationships": RELS}), encoding="utf-8")
    mixed = tmp_path / "mixed.ndjson"
    _ndjson(mixed, NODES + RELS)
    folder = tmp_path / "graph"
    folder.mkdir()
    _ndjson(folder / "nodes.ndjson", NODES)
    _ndjson(folder / "rels.ndjson", RELS)
    (folder / "meta.json").write_text(
        json.dumps({"exported_at": "x", "upstream_meta": {"complete": True, "node_count": 2,
                                                           "relationship_count": 1}}),
        encoding="utf-8",
    )
    edges = tmp_path / "edges.json"
    edges.write_text(json.dumps({"nodes": NODES, "edges": RELS}), encoding="utf-8")

    for path in (single, mixed, folder, edges):
        doc = read_export(path)
        assert len(doc["nodes"]) == 2 and len(doc["relationships"]) == 1, path.name
    assert read_export(folder)["summary"]["node_count"] == 2


def test_half_of_a_folder_export_is_refused_with_directions(tmp_path):
    nodes = tmp_path / "nodes.ndjson"
    _ndjson(nodes, NODES)
    with pytest.raises(NotAGraphExport, match="point at the folder"):
        read_export(nodes)


def test_a_line_that_is_neither_node_nor_relationship_is_refused(tmp_path):
    bad = tmp_path / "bad.ndjson"
    _ndjson(bad, [*NODES, {"rows": 1}])
    with pytest.raises(NotAGraphExport, match="neither a node nor a relationship"):
        read_export(bad)


# ---------------------------------------------------------------------------
# the v2 export itself
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def v2():
    from pricing.graph.json_source import GraphJsonSource
    from pricing.profiles import get_profile

    profile = get_profile("axiom_v2")
    return profile, GraphJsonSource(V2_EXPORT, profile=profile)


@needs_v2
def test_v2_runs_and_its_line_amounts_add_up(v2):
    from pricing.graph import validation

    profile, src = v2
    report = validation.validate(src, profile)
    assert report.ok, str(report)
    assert not any(f.check == "line amounts do not add up" for f in report.findings)


@needs_v2
def test_v2_payment_discounts_stay_whole_on_the_order(v2):
    """Every order-level payment discount reaches the order, and no product line."""
    from pricing.engine import waterfall as wf
    from pricing.profiles import _axiom_in_currency

    profile, src = v2
    lines, keys, _ = wf.build(src, profile)
    _, _, components = _axiom_in_currency(
        src, src.table("orders"), src.table("order_lines"), src.table("price_components"), "SAR"
    )
    paid = components[components["step_name"] == "payment_discount"]["amount"].sum()
    returns = components[components["step_name"] == "returns_credit"]["amount"].sum()
    assert keys["off_invoice_leakage"].sum() == pytest.approx(-(paid + returns), rel=1e-9)
    assert "off_invoice_leakage" not in lines.columns


@needs_v2
def test_v2_euro_orders_are_converted_before_costs_are_subtracted(v2):
    """Euro prices minus SAR costs is not a margin. Both currencies must land alike."""
    from pricing.engine import waterfall as wf

    profile, src = v2
    w = wf.compute(src, profile=profile)
    keys = w.keys.copy()
    currency = w.facts.drop_duplicates("order_id").set_index("order_id")["order_currency"]
    keys["order_currency"] = keys["order_id"].map(currency)
    assert set(keys["currency_code"]) == {"SAR"}
    margin = keys.groupby("order_currency").apply(
        lambda g: g["pocket_margin"].sum() / g["pocket_price"].sum(), include_groups=False
    )
    assert margin["EUR"] == pytest.approx(margin["SAR"], abs=0.05)


@needs_v2
def test_v2_win_data_uses_only_what_was_known_when_quoting(v2):
    profile, src = v2
    data = profile.win_data(src)
    frame = data.frame
    comp = src.table("competitors")
    late = comp.merge(frame[["deal_id", "issued_at"]], left_on="opportunity_id", right_on="deal_id")
    late = late[late["learned_at"] > late["issued_at"]]
    known_only_late = set(late["deal_id"]) - set(
        comp.merge(frame[["deal_id", "issued_at"]], left_on="opportunity_id", right_on="deal_id")
        .query("learned_at <= issued_at")["deal_id"]
    )
    flagged = frame.set_index("deal_id").loc[sorted(known_only_late), "competitors_known"]
    assert (flagged == 0).all(), "a competitor learned after the quote leaked into the model"
    assert set(frame["won"].unique()) <= {0, 1}
    assert data.priced["price_origin"].isin(["model_suggested", "random_test"]).all()
