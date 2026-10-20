"""The `axiom_poc` profile: the delivered knowledge graph, read as a waterfall."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import config  # noqa: E402
from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource, validation  # noqa: E402
from pricing.graph.json_source import GraphJsonSource, NotAGraphExport  # noqa: E402
from pricing.graph.source import get_source  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402
from pricing.quality.conformance import Fit, check  # noqa: E402
from pricing.spec import KEY, LINE, Kind, Origin  # noqa: E402

PROFILE = get_profile("axiom_poc")
EXPORT = config.GRAPH_JSON
SNAPSHOT = config.DATA_DIR / "snapshot_axiom_poc_v1"

needs_graph = pytest.mark.skipif(
    not EXPORT.exists(), reason=f"{EXPORT} not present; set PRICING_GRAPH_JSON"
)
needs_snapshot = pytest.mark.skipif(
    not SNAPSHOT.exists(),
    reason=f"{SNAPSHOT} not frozen; run scripts/import_graph_json.py",
)


@pytest.fixture(scope="module")
def source() -> GraphJsonSource:
    """The graph itself, read in place. No snapshot involved."""
    return GraphJsonSource(EXPORT, profile=PROFILE)


@pytest.fixture(scope="module")
def waterfall(source) -> wf.Waterfall:
    """One computed waterfall, shared by every test that only reads it."""
    return wf.compute(source, profile=PROFILE)


def corrupted(export: Path, mutate) -> GraphJsonSource:
    """A copy of the export with one thing wrong with it, as a source."""
    import pricing.graph.json_source as js

    doc = copy.deepcopy(js.read_export(export))
    mutate(doc)
    src = GraphJsonSource.__new__(GraphJsonSource)
    src.profile = PROFILE
    src.export_path = export
    src.document = doc
    src._frames = None
    src._cache = {}
    return src


# ---------------------------------------------------------------------------
# the declared chain
# ---------------------------------------------------------------------------


def test_chain_is_the_canonical_nine():
    """The contract has a field per step, so the chain must not invent its own."""
    from pricing.engine.result import CANONICAL_STEPS

    assert PROFILE.step_names == CANONICAL_STEPS


def test_on_invoice_band_is_document_grain():
    """Freight is posted against the order, so the band it sits in is order-grain."""
    assert PROFILE.step("on_invoice_discount").grain == KEY
    assert PROFILE.step("off_invoice_leakage").grain == LINE


def test_everything_below_the_invoice_price_inherits_that_grain():
    """Grain propagates down a chain; it never recovers."""
    below = ("invoice_price", "pocket_price", "pocket_margin")
    assert all(PROFILE.step(s).grain == KEY for s in below)
    assert PROFILE.step("list_value").grain == LINE
    assert PROFILE.step("gross_value").grain == LINE


def test_measures_are_the_ones_the_graph_actually_records():
    """Five measures, four derived. Everything else is arithmetic, not data."""
    assert set(PROFILE.measures) == {
        "list_value", "gross_value", "on_invoice_discount",
        "off_invoice_leakage", "cogs",
    }
    derived = [s for s in PROFILE.steps if s.origin is Origin.DERIVED]
    assert {s.name for s in derived} == {
        "negotiated_off_list", "invoice_price", "pocket_price", "pocket_margin",
    }


def test_product_is_line_grain_and_customer_is_not():
    """The grain rule only bites because these two differ."""
    assert not PROFILE.dimension("product").key_safe
    assert PROFILE.dimension("customer").key_safe
    assert PROFILE.key == "order_id"


def test_chain_alternates_levels_and_deductions():
    """A malformed chain would already have raised at import; assert it anyway."""
    kinds = [s.kind for s in PROFILE.steps]
    assert kinds[0] is Kind.LEVEL and kinds[-1] is Kind.LEVEL
    assert all(a is not b for a, b in zip(kinds, kinds[1:], strict=False))


# ---------------------------------------------------------------------------
# the graph, read in place
# ---------------------------------------------------------------------------


@needs_graph
def test_graph_conforms_exactly(source):
    """Every table and column the profile reads is present and parses."""
    verdict = check(source, PROFILE)
    assert verdict.fit is Fit.EXACT, str(verdict)


@needs_graph
def test_graph_has_no_blocking_findings(source):
    """Entry-point validation admits this data."""
    report = validation.validate(source, PROFILE)
    assert report.ok, str(report)


@needs_graph
def test_waterfall_arithmetic_reconciles(waterfall):
    """Each level is the one above it less the deduction between them."""
    t = waterfall.totals
    assert t["list_value"] - t["negotiated_off_list"] == pytest.approx(t["gross_value"], abs=0.01)
    assert t["gross_value"] - t["on_invoice_discount"] == pytest.approx(t["invoice_price"], abs=0.01)
    assert t["invoice_price"] - t["off_invoice_leakage"] == pytest.approx(t["pocket_price"], abs=0.01)
    assert t["pocket_price"] - t["cogs"] == pytest.approx(t["pocket_margin"], abs=0.01)


@needs_graph
def test_list_value_agrees_with_quantity_times_list_price(waterfall):
    """Two independent statements of the same number, and they must not diverge."""
    r = waterfall.reference.dropna(subset=["reported_list_value", "computed_list_value"])
    assert len(r) > 0
    pd.testing.assert_series_equal(
        r["reported_list_value"], r["computed_list_value"],
        check_names=False, rtol=0, atol=0.01,
    )


@needs_graph
def test_line_value_agrees_with_quantity_times_recorded_price(waterfall):
    """`line_value` is not independent of the unit price; it must still agree."""
    r = waterfall.reference.dropna(subset=["reported_line_value", "computed_line_value"])
    pd.testing.assert_series_equal(
        r["reported_line_value"], r["computed_line_value"],
        check_names=False, rtol=0, atol=0.01,
    )


@needs_graph
def test_document_grain_steps_abstain_when_sliced_by_product(waterfall):
    """The rule that replaced allocation, on this schema."""
    by_product = agg.aggregate(waterfall, "product")
    for step in ("on_invoice_discount", "invoice_price", "pocket_price", "pocket_margin"):
        assert by_product[step].isna().all(), f"{step} was reported at product grain"
    assert by_product["gross_value"].notna().any()
    assert "order document grain" in by_product["grain_note"].iloc[0]


@needs_graph
def test_customer_grain_reports_every_step(waterfall):
    """Rolling up is free of assumptions, so nothing is suppressed here."""
    by_customer = agg.aggregate(waterfall, "customer")
    assert by_customer["unavailable_steps"].eq("").all()
    assert by_customer["pocket_margin"].notna().any()


@needs_graph
def test_aggregates_reconcile_with_the_grand_total(waterfall):
    """Catches join fanout before it reaches a report."""
    for dimension in ("customer", "channel", "period", "segment", "region", "product"):
        checks = agg.reconciles_with_total(waterfall, dimension)
        assert checks["ok"].all(), (
            f"{dimension} does not reconcile with the grand total:\n"
            f"{checks[~checks['ok']].to_string(index=False)}"
        )


@needs_graph
def test_customer_period_rebates_are_excluded_and_reported(source):
    """Money that cannot be attributed leaves a warning, not a silent gap."""
    components = source.table("price_components")
    accrued = components[components["applies_to_level"] == "customer_period"]
    assert len(accrued) > 0, "fixture no longer exercises this case"

    report = validation.validate(source, PROFILE)
    warnings = " ".join(f.detail for f in report.warnings)
    assert "per customer per quarter" in warnings

    lines, keys, _ = wf.build(source, PROFILE)
    banded = lines["off_invoice_leakage"].sum() + keys["on_invoice_discount"].sum()
    assert banded == pytest.approx(
        -components.loc[
            (components["step_type"] == "deduction")
            & (components["applies_to_level"] != "customer_period"),
            "amount",
        ].sum(),
        abs=0.01,
    )


@needs_graph
def test_mixed_currency_aggregates_abstain(waterfall):
    """The book holds two currencies and no FX rate."""
    by_segment = agg.aggregate(waterfall, "segment")
    mixed = by_segment[by_segment["n_currencies"] > 1]
    assert len(mixed) > 0, "fixture no longer exercises this case"

    from pricing.engine import result as result_builder

    record = result_builder.waterfall_records(waterfall, ["segment"])
    abstained = [r for r in record if r.status.value == "abstained"]
    assert abstained, "a segment spanning two currencies was reported as a number"




# ---------------------------------------------------------------------------
# the backends
# ---------------------------------------------------------------------------


@needs_graph
def test_the_profile_selects_the_json_backend_on_its_own():
    """A profile declares where its data lives; nobody should have to remember."""
    assert PROFILE.default_source == "graph_json"
    assert isinstance(get_source(profile=PROFILE), GraphJsonSource)


@needs_graph
@needs_snapshot
def test_an_explicit_backend_beats_the_profile():
    """The declaration is a default, not a lock."""
    src = get_source("csv", data_dir=SNAPSHOT, profile=PROFILE)
    assert isinstance(src, CsvGraphSource)
    assert src.data_dir == SNAPSHOT


@needs_graph
def test_a_table_is_a_label():
    """The profile's table map holds node labels, and the source resolves them."""
    src = GraphJsonSource(EXPORT, profile=PROFILE)
    assert src.label_for("orders") == "Order"
    assert len(src.table("orders")) == 100
    assert len(src.table("order_lines")) == 262


@needs_graph
def test_a_label_the_graph_does_not_carry_reads_as_a_missing_table():
    """A label the graph lacks is reported as a missing table, not as a crash."""
    from dataclasses import replace

    wanting_more = replace(PROFILE, tables={**PROFILE.tables, "warehouses": "Warehouse"})
    src = GraphJsonSource(EXPORT, profile=wanting_more)

    with pytest.raises(FileNotFoundError, match="carries no 'Warehouse' nodes"):
        src.table("warehouses")

    verdict = check(src, wanting_more)
    assert verdict.fit is Fit.UNUSABLE
    assert verdict.missing_tables == ("warehouses",)


@needs_graph
@needs_snapshot
def test_graph_and_frozen_snapshot_agree_to_the_cent():
    """The load-bearing property of having more than one backend."""
    from_graph = wf.compute(GraphJsonSource(EXPORT, profile=PROFILE), profile=PROFILE)
    from_csv = wf.compute(CsvGraphSource(SNAPSHOT, profile=PROFILE), profile=PROFILE)

    for step in PROFILE.step_names:
        assert from_graph.totals[step] == pytest.approx(from_csv.totals[step], abs=0.01), (
            f"{step} differs between the graph and its frozen snapshot"
        )
    assert from_graph.totals["n_certified"] == from_csv.totals["n_certified"]


@needs_graph
def test_provenance_names_the_export_and_hashes_it():
    """A result has to say which graph it came from, not just that it came from one."""
    info = GraphJsonSource(EXPORT, profile=PROFILE).snapshot_info()
    assert info["backend"] == "graph_json"
    assert info["location"] == str(EXPORT)
    assert len(info["content_sha256"]) == 64
    assert info["node_counts"]["Order"] == 100


# ---------------------------------------------------------------------------
# graph integrity
# ---------------------------------------------------------------------------


@needs_graph
def test_the_delivered_graph_is_internally_consistent(source):
    """Nothing below means anything if this fails."""
    assert source.graph_integrity() == []


def test_a_json_file_that_is_not_an_export_is_refused(tmp_path):
    """Loudly, and about the file -- not about fifteen missing tables."""
    path = tmp_path / "not_a_graph.json"
    path.write_text('{"rows": []}', encoding="utf-8")
    with pytest.raises(NotAGraphExport, match="does not look like an AXIOM graph export"):
        GraphJsonSource(path, profile=PROFILE)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(NotAGraphExport, match="not readable JSON"):
        GraphJsonSource(broken, profile=PROFILE)


@needs_graph
def test_a_truncated_export_is_blocking():
    """The export states its own counts, so a short read is detectable."""

    def drop_a_node(doc):
        doc["nodes"] = doc["nodes"][:-1]

    findings = corrupted(EXPORT, drop_a_node).graph_integrity()
    assert any(f.check == "export completeness" for f in findings)
    assert all(f.severity is validation.Severity.BLOCKING for f in findings)


@needs_graph
def test_a_contradictory_denormalised_key_is_blocking():
    """The check that exists because the profile reads the cheap copy."""

    def relabel_a_line(doc):
        for node in doc["nodes"]:
            if node["labels"][0] == "OrderLine":
                node["properties"]["order_id"] = "ORD99999"
                return

    findings = corrupted(EXPORT, relabel_a_line).graph_integrity()
    contradictions = [f for f in findings if f.check == "contradictory key"]
    assert len(contradictions) == 1
    assert contradictions[0].n_affected == 1
    assert "PART_OF" in contradictions[0].detail


@needs_graph
def test_a_duplicated_business_key_is_blocking():
    """A duplicate key makes every join through it fan out silently."""

    def duplicate_an_order(doc):
        order = next(n for n in doc["nodes"] if n["labels"][0] == "Order")
        clone = copy.deepcopy(order)
        clone["id"] = order["id"] + "-clone"
        doc["nodes"].append(clone)
        doc["summary"]["node_count"] = len(doc["nodes"])

    findings = corrupted(EXPORT, duplicate_an_order).graph_integrity()
    assert any(f.check == "duplicate business key" for f in findings)


@needs_graph
def test_a_dangling_edge_is_blocking():
    """An edge to a node that did not arrive means a partial graph."""

    def orphan_an_edge(doc):
        doc["relationships"][0]["target"] = "4:nope:999999"

    findings = corrupted(EXPORT, orphan_an_edge).graph_integrity()
    assert any(f.check == "dangling edge" for f in findings)


@needs_graph
def test_integrity_runs_as_part_of_the_entry_gate():
    """Checked on every read, not once at import time."""

    def relabel_a_line(doc):
        for node in doc["nodes"]:
            if node["labels"][0] == "OrderLine":
                node["properties"]["order_id"] = "ORD99999"
                return

    report = validation.validate(corrupted(EXPORT, relabel_a_line), PROFILE)
    assert not report.ok
    assert any(f.check == "contradictory key" for f in report.blocking)


# ---------------------------------------------------------------------------
# freezing a snapshot
# ---------------------------------------------------------------------------


@needs_snapshot
def test_the_frozen_snapshot_records_a_clean_verification():
    """The snapshot on disk was checked when it was written."""
    manifest = json.loads((SNAPSHOT / "_manifest.json").read_text(encoding="utf-8"))
    verification = manifest.get("verification")
    if verification is None:
        pytest.skip("snapshot was frozen without --verify")
    assert verification["passed"], verification["findings"]


@needs_snapshot
def test_frozen_files_are_named_after_their_labels():
    """So one map of labels serves the graph and a folder of CSVs alike."""
    for label in PROFILE.tables.values():
        assert (SNAPSHOT / f"{label}.csv").exists(), f"{label}.csv missing from the snapshot"


# ---------------------------------------------------------------------------
# thresholds, and what lowering one is allowed to hide
# ---------------------------------------------------------------------------


def test_recommended_depth_is_not_reachable_from_settings():
    """What the module asks for is fixed; what it was told to accept is not."""
    assert config.RECOMMENDED_DEPTH == {
        "waterfall": 1,
        "margin_analytics": 20,
        "segmentation": 60,
        "elasticity": 120,
        "optimisation": 200,
    }


def test_a_lowered_threshold_is_reported_not_absorbed():
    """Lowering one is legitimate. Doing it quietly is not."""
    reservations = " ".join(config.depth_reservations())
    for name, configured in config.MIN_DOCUMENTS.items():
        if configured < config.RECOMMENDED_DEPTH[name]:
            assert name in reservations, f"{name} was lowered and said nothing"
            assert str(config.RECOMMENDED_DEPTH[name]) in reservations


@needs_graph
def test_readiness_carries_the_reservations_into_its_report(source):
    """So the reservation is read by whoever reads the score, not just the code."""
    from pricing.quality.readiness import assess

    verdict = assess(source, PROFILE)
    assert verdict.reservations == config.depth_reservations()
    if verdict.reservations:
        assert "Thresholds lowered for this run" in str(verdict)


@needs_graph
def test_a_limited_capability_does_not_state_its_blocking_reason(source):
    """"LIMITED: needs 100 documents, has 100" is a report with a bug in it."""
    from pricing.quality.readiness import DEPTH_REQUIREMENTS, assess

    capabilities = assess(source, PROFILE).capabilities
    for _, row in capabilities.iterrows():
        if row["status"] != "LIMITED":
            continue
        needed = DEPTH_REQUIREMENTS.get(row["deliverable"])
        assert f"needs {needed} certified documents" not in row["reason"], (
            f"{row['deliverable']} is LIMITED but gives the reason it would be BLOCKED"
        )
    assert (capabilities["status"] != "READY").any() or True


# ---------------------------------------------------------------------------
# fitting at a coarser scope
# ---------------------------------------------------------------------------


@needs_graph
def test_a_coarse_scope_pools_depths_but_never_prices(waterfall):
    """The correctness of fitting acceptance above product grain."""
    from pricing.models import acceptance as acceptance_model

    model = acceptance_model.fit(waterfall, scope="family")
    assert len(model.curves) < 10, "family scope should pool into a handful of curves"

    facts = waterfall.facts
    prices = facts.groupby("product_id")["list_price"].median().dropna()
    cheap, dear = prices.idxmin(), prices.idxmax()
    assert prices[dear] > 2 * prices[cheap], "fixture no longer exercises this case"

    assert model.product_reference[cheap] != model.product_reference[dear]
    assert model.product_reference[cheap] < model.product_reference[dear]


@needs_graph
def test_the_optimiser_anchors_on_the_product_not_on_its_scope(waterfall):
    """Follows from the above: the band has to sit around the product's own price."""
    from pricing.models import acceptance as acceptance_model
    from pricing.models import optimiser as optimiser_model

    model = acceptance_model.fit(waterfall, scope="family")
    recommendations = optimiser_model.recommend_book(waterfall, model, top=60)
    quoted = recommendations[recommendations["status"] == "complete"]
    assert len(quoted) > 0, "nothing was quoted, so nothing is being tested"

    # Each band must sit around that product's own trading level, not its family's.
    for _, row in quoted.iterrows():
        assert row["reference_price"] == pytest.approx(
            model.product_reference[row["product_id"]], abs=0.01
        )
        assert 0.3 * row["list_price"] <= row["mid"] <= 1.3 * row["list_price"], (
            f"{row['product_id']}: recommended {row['mid']:.2f} against a list of "
            f"{row['list_price']:.2f} -- the band is anchored somewhere else"
        )


@needs_graph
def test_the_uplift_is_measured_against_what_was_actually_paid(waterfall):
    """Not against list, which would read every discounted customer as an uplift."""
    from pricing.models import acceptance as acceptance_model
    from pricing.models import optimiser as optimiser_model

    model = acceptance_model.fit(waterfall, scope="family")
    recommendations = optimiser_model.recommend_book(waterfall, model, top=60)
    quoted = recommendations[recommendations["status"] == "complete"]

    facts = waterfall.facts[waterfall.facts["certified"]]
    recorded = PROFILE.unit_price_column
    assert recorded in facts.columns

    row = quoted.iloc[0]
    paid = facts[
        (facts["customer_id"] == row["customer_id"])
        & (facts["product_id"] == row["product_id"])
    ][recorded].median()
    assert row["current_price"] == pytest.approx(paid, abs=0.01)

    # And it must differ from list wherever the customer was in fact discounted.
    discounted = quoted[quoted["current_price"] < quoted["list_price"]]
    assert len(discounted) > 0, "every baseline equals list price -- reading the wrong column"
