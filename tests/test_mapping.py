"""Source-mapping tests: parsing the mapping, and the fixture generated from it."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import validation  # noqa: E402
from pricing.mapping.fixture import FixtureSpec, build_tables, write  # noqa: E402
from pricing.mapping.r2rml import SourceMapping, find_mapping, template_columns  # noqa: E402
from pricing.mapping.workbook import WorkbookSource  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402
from pricing.quality.readiness import assess  # noqa: E402

MAPPING_PATH = find_mapping(Path(__file__).resolve().parents[2])

pytestmark = pytest.mark.skipif(
    MAPPING_PATH is None, reason="no axiom-source-mapping*.json alongside the repo"
)


@pytest.fixture(scope="module")
def mapping() -> SourceMapping:
    """The client's source mapping, parsed."""
    return SourceMapping.load(MAPPING_PATH)


@pytest.fixture(scope="module")
def workbook(mapping, tmp_path_factory) -> Path:
    """A tiny workbook generated from that mapping."""
    out = tmp_path_factory.mktemp("fixture") / "tiny.xlsx"
    return write(FixtureSpec(mapping=mapping, out_path=out))


@pytest.fixture(scope="module")
def source(mapping, workbook) -> WorkbookSource:
    """That workbook, read through the dairy profile's table lookup."""
    return WorkbookSource(workbook, mapping, tables=get_profile("dairy_al_kharj").tables)


@pytest.fixture(scope="module")
def dairy(source):
    """The waterfall the engine computes over the generated workbook."""
    return wf.compute(source, profile=get_profile("dairy_al_kharj"))


# -- parsing -----------------------------------------------------------------


def test_the_mapping_parses_into_sheets_and_classes(mapping):
    """The mapping is a schema; parsing it must not need the workbook to exist."""
    assert mapping.format.startswith("axiom-r2rml")
    assert mapping.workbook.endswith(".xlsx")
    assert len(mapping.sheets) > 10
    assert "DairyProduct" in mapping.classes


def test_a_class_declared_on_many_sheets_has_one_defining_sheet(mapping):
    """`Region` is a join stub nearly everywhere; exactly one sheet defines it."""
    assert len(mapping.for_class("Region")) > 3
    assert mapping.primary_sheet_for("Region") in mapping.sheets


def test_join_columns_absent_from_a_sheet_are_not_joinable(mapping):
    """A relationship discharged as 'none-on-this-sheet' carries no key to join on."""
    unjoinable = [
        rel
        for cm in mapping.class_maps
        for rel in cm.relations
        if rel.discharge == "none-on-this-sheet"
    ]
    assert unjoinable, "fixture assumption broken"
    assert not any(rel.joinable for rel in unjoinable)


def test_sheet_columns_include_identity_property_and_join_keys(mapping):
    """The derived column set is exactly the surface the graph depends on."""
    cols = mapping.sheet_columns("24_Financial_Plan")
    assert {"Scenario_ID", "SKU_ID", "Region_ID", "Planned_Revenue_SAR"} <= set(cols)


def test_a_sheet_that_maps_no_columns_falls_back_to_its_templates(mapping):
    """`03_Calendar` builds nodes purely from templates; it must not come out empty."""
    assert mapping.sheet_columns("03_Calendar", infer=False) == ()
    assert set(mapping.sheet_columns("03_Calendar")) >= {"Date"}


def test_template_placeholders_become_column_names():
    """`{year}-{month}` implies two columns, not one."""
    assert template_columns("{year}-{month}") == ("Year", "Month")
    assert template_columns("{Sales_Order_Line_ID}") == ("Sales_Order_Line_ID",)
    assert template_columns("") == ()


# -- the generated fixture ---------------------------------------------------


def test_the_fixture_has_every_sheet_the_mapping_declares(mapping):
    """Structure is the deliverable; a missing sheet is a missing test surface."""
    tables = build_tables(FixtureSpec(mapping=mapping, out_path=Path("unused.xlsx")))
    assert set(tables) == set(mapping.sheets)
    for sheet, df in tables.items():
        assert list(df.columns) == list(mapping.sheet_columns(sheet)), sheet
        assert len(df) >= 1, f"{sheet} generated no rows"


def test_the_fixture_is_deterministic(mapping):
    """Same mapping, same seed, same bytes -- or it cannot be a test fixture."""
    spec = FixtureSpec(mapping=mapping, out_path=Path("unused.xlsx"))
    first = build_tables(spec)["24_Financial_Plan"]
    second = build_tables(spec)["24_Financial_Plan"]
    pd.testing.assert_frame_equal(first, second)


def test_generated_foreign_keys_resolve(mapping):
    """A fixture whose keys dangle would fail validation for a reason it invented."""
    tables = build_tables(FixtureSpec(mapping=mapping, out_path=Path("unused.xlsx")))
    products = set(tables["08_Products"]["SKU_ID"])
    regions = set(tables["04_Regions"]["Region_ID"])
    customers = set(tables["09_Customers"]["Customer_ID"])

    for sheet in ("19_Sales_Orders", "23_Invoices", "24_Financial_Plan"):
        df = tables[sheet]
        assert set(df["SKU_ID"]) <= products, sheet
        assert set(df["Region_ID"]) <= regions, sheet
        if "Customer_ID" in df.columns:
            assert set(df["Customer_ID"]) <= customers, sheet


def test_the_financial_plan_arithmetic_holds(mapping):
    """The engine's reconciliation must be testing the engine, not the fixture."""
    plan = build_tables(FixtureSpec(mapping=mapping, out_path=Path("unused.xlsx")))[
        "24_Financial_Plan"
    ]
    computed = (
        plan["Planned_Revenue_SAR"]
        - plan["Planned_COGS_SAR"]
        - plan["Planned_Freight_SAR"]
        - plan["Planned_Returns_SAR"]
    )
    assert (computed - plan["Planned_Contribution_Margin_SAR"]).abs().max() < 0.01


def test_the_dashboard_is_the_true_total_of_the_plan(mapping):
    """A cross-sheet roll-up the engine can actually check."""
    tables = build_tables(FixtureSpec(mapping=mapping, out_path=Path("unused.xlsx")))
    plan, dash = tables["24_Financial_Plan"], tables["26_Dashboard"]
    base = plan[plan["Scenario_ID"] == plan["Scenario_ID"].iloc[0]]
    assert abs(base["Planned_Revenue_SAR"].sum() - dash["Revenue"].iloc[0]) < 0.01


def test_the_invoice_sheet_carries_no_money(mapping):
    """The finding that governs the whole onboarding, asserted rather than asserted-in-prose."""
    cols = mapping.sheet_columns("23_Invoices")
    assert "Invoice_Line_ID" in cols
    money = [c for c in cols if any(t in c for t in ("Amount", "SAR", "Price", "Value"))]
    assert money == [], f"the mapping does expose money at invoice grain: {money}"


# -- reading it back ---------------------------------------------------------


def test_the_workbook_source_serves_logical_table_names(source):
    """A profile asks for `financial_plan`, not for `24_Financial_Plan`."""
    plan = source.table("financial_plan")
    assert "Planned_Revenue_SAR" in plan.columns
    assert source.missing_sheets() == ()
    assert source.missing_columns() == {}


def test_the_workbook_source_names_the_sheet_it_could_not_find(source):
    """A mapping/workbook mismatch must be legible, not a pandas ValueError."""
    with pytest.raises(KeyError, match="not in"):
        source.sheet("99_Does_Not_Exist")


def test_provenance_covers_the_mapping_as_well_as_the_workbook(source):
    """Change the mapping and the same workbook is a different read."""
    info = source.snapshot_info()
    assert info["backend"] == "workbook"
    assert info["content_sha256"]
    assert info["mapping"].endswith(".json")


def test_the_generated_workbook_passes_the_entry_gate(source):
    """Structurally sound, so the engine must agree to run on it."""
    report = validation.validate(source, get_profile("dairy_al_kharj"))
    assert report.ok, f"blocking findings:\n{report}"
    assert any(f.check == "money grain" for f in report.warnings)


# -- the waterfall on it -----------------------------------------------------


def test_the_dairy_waterfall_reconciles_with_the_sheets_own_margin(dairy):
    """The plan states its contribution margin; the engine must rederive it."""
    reference = dairy.reference.set_index("transaction_id")["reported_pocket_margin"]
    computed = dairy.keys.set_index("transaction_id")["pocket_margin"]
    assert (computed - reference).abs().max() < 0.01


def test_the_dairy_chain_has_no_document_grain_steps(dairy):
    """Every dimension is constant within a plan row, so nothing ever abstains."""
    by_product = agg.aggregate(dairy, "product")
    assert by_product["grain_note"].iloc[0] == ""
    for step in dairy.profile.step_names:
        assert by_product[step].notna().any(), step


def test_the_dairy_waterfall_slices_reconcile(dairy):
    """A groupby that does not sum back to the total has fanned out a join."""
    for dimension in ("product", "region", "scenario"):
        rec = agg.reconciles_with_total(dairy, dimension)
        assert rec["ok"].all(), f"{dimension}:\n{rec}"


def test_steps_the_mapping_cannot_support_are_absent_with_a_reason(dairy):
    """Not zero, not null -- absent, and explained."""
    profile = dairy.profile
    for step in ("list_value", "on_invoice_discount", "invoice_price"):
        assert not profile.has(step)
        assert step in profile.absent_steps
        assert step not in dairy.keys.columns


def test_margin_analytics_run_on_a_profile_with_no_customer(dairy):
    """Not every schema records a customer, and the views must not assume one."""
    from pricing.analytics import margin

    assert dairy.profile.dimension("customer") is None
    views = margin.run_all(dairy)
    assert len(views["summary"])
    assert len(views["margin_quadrants"])
    assert "leakage_by_product" in views


def test_the_pipeline_runs_end_to_end_on_the_generated_workbook(source):
    """One mapping, one profile, and the same contract Track B already reads."""
    from pricing.contracts import schema as C
    from pricing.engine import pipeline

    run = pipeline.run(
        source, track=False, with_models=True, profile=get_profile("dairy_al_kharj")
    )
    assert run.ok
    assert not C.validate(run.result)
    assert run.result.waterfalls
    assert not run.result.segments, "no customer dimension, so nothing to segment"
    assert set(run.models["skipped"]) == {"segmentation", "elasticity", "optimisation"}

    absent = {
        w.key["product_id"]: w.list_value
        for w in run.result.waterfalls
        if "product_id" in w.key
    }
    for money in absent.values():
        assert money.amount is None
        assert "no list price is mapped" in money.note


def test_readiness_is_honest_about_what_this_mapping_cannot_do(source):
    """Good field coverage must not buy a READY when whole deliverables are impossible."""
    verdict = assess(source, get_profile("dairy_al_kharj"))
    statuses = dict(zip(verdict.capabilities["deliverable"], verdict.capabilities["status"], strict=False))
    reasons = dict(zip(verdict.capabilities["deliverable"], verdict.capabilities["reason"], strict=False))

    assert statuses["waterfall"] == "READY"
    assert statuses["segmentation"] == "BLOCKED"
    assert "customer" in reasons["segmentation"]
    assert verdict.band.value == "limited", "a blocked deliverable cannot be READY"
