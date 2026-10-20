"""Conformance tests: will swapped-in data actually run, and does the check say why?"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.graph import CsvGraphSource  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402
from pricing.quality.conformance import Fit, check, describe, identify  # noqa: E402

B2B = get_profile("b2b_industrial")
DAIRY = get_profile("dairy_al_kharj")


@pytest.fixture
def snapshot(tmp_path) -> Path:
    """A private copy of the synthetic snapshot, safe to damage."""
    out = tmp_path / "snap"
    shutil.copytree(CsvGraphSource().data_dir, out)
    return out


def _rewrite(path: Path, fn) -> None:
    """Load a CSV, transform it, write it back."""
    df = pd.read_csv(path)
    fn(df).to_csv(path, index=False)


# -- the happy path ----------------------------------------------------------


def test_an_untouched_snapshot_is_an_exact_fit(snapshot):
    """The shipped data must pass its own checker, or the checker is wrong."""
    verdict = check(snapshot, B2B)
    assert verdict.fit is Fit.EXACT
    assert verdict.score == 100.0
    assert not verdict.missing_tables and not verdict.missing_columns
    assert any("fact rows" in n for n in verdict.notes), "the extraction should have run"


def test_the_checker_never_computes_a_waterfall(snapshot):
    """It is a schema check. Cheap enough to run on every snapshot that lands."""
    verdict = check(snapshot, B2B)
    assert not hasattr(verdict, "totals")
    assert all(t.n_rows >= 0 for t in verdict.tables)


# -- the three ways a hand-off breaks ----------------------------------------


def test_a_missing_file_is_named(snapshot):
    """Not "something went wrong" -- which file, and what it was expected for."""
    (snapshot / "channels.csv").unlink()
    verdict = check(snapshot, B2B)

    assert verdict.fit is Fit.UNUSABLE
    assert verdict.missing_tables == ("channels",)
    assert "channels.csv" in str(verdict)


def test_a_renamed_column_is_named(snapshot):
    """The quiet one: pandas returns the frame, the merge yields NaN, nothing raises."""
    _rewrite(snapshot / "products.csv", lambda df: df.rename(columns={"unit_cost": "cost"}))
    verdict = check(snapshot, B2B)

    assert verdict.fit is Fit.UNUSABLE
    assert verdict.missing_columns == {"products": ("unit_cost",)}


def test_a_number_exported_as_text_is_caught(snapshot):
    """`"59,440.02" > "999"` is False, and every comparison silently inverts."""
    _rewrite(
        snapshot / "transactions.csv",
        lambda df: df.assign(gross_amount=df["gross_amount"].map("{:,.2f}".format)),
    )
    verdict = check(snapshot, B2B)

    assert verdict.fit is Fit.USABLE, "it will run -- that is exactly the problem"
    assert verdict.untyped_columns == {"transactions": ("gross_amount",)}
    assert "would not parse" in str(verdict).lower()


def test_a_blank_column_is_a_gap_not_a_parse_failure(snapshot):
    """The engine already reports gaps as UNAVAILABLE; do not double-report them."""
    _rewrite(snapshot / "products.csv", lambda df: df.assign(unit_cost=None))
    verdict = check(snapshot, B2B)

    assert verdict.untyped_columns == {}
    assert verdict.fit is Fit.EXACT


def test_a_column_the_extraction_needs_is_caught_by_running_it(snapshot):
    """Declared types are a lower bound, so the check also runs the extraction."""
    _rewrite(
        snapshot / "transaction_lines.csv",
        lambda df: df.rename(columns={"product_id": "sku"}),
    )
    verdict = check(snapshot, B2B)

    assert verdict.fit is Fit.UNUSABLE
    assert verdict.engine_error, "a column only the extraction reads must still be caught"


# -- picking a profile -------------------------------------------------------


def test_the_wrong_profile_is_rejected_rather_than_half_read(snapshot):
    """Pointing the dairy profile at the distributor snapshot must fail loudly."""
    verdict = check(snapshot, DAIRY)
    assert verdict.fit is Fit.UNUSABLE
    assert len(verdict.missing_tables) == len(DAIRY.tables)


def test_identify_ranks_the_profiles_by_how_well_they_fit(snapshot):
    """"I have a folder and I do not know what it is" is a real question."""
    ranked = identify(snapshot)
    assert ranked.iloc[0]["profile"] == "b2b_industrial"
    assert ranked.iloc[0]["fit"] == "exact"
    assert ranked.iloc[-1]["score"] < ranked.iloc[0]["score"]
    assert ranked.iloc[-1]["first_problem"], "a losing profile must say what was missing"


def test_a_path_that_does_not_exist_is_a_verdict_not_an_exception():
    """A gate that crashes cannot gate anything."""
    verdict = check("does/not/exist", B2B)
    assert verdict.fit is Fit.UNUSABLE
    assert "no such file or directory" in verdict.engine_error


# -- what to ask the client for ----------------------------------------------


def test_describe_names_a_file_and_its_typed_columns():
    """The sheet you send upstream."""
    spec = describe(B2B).set_index("table")
    assert spec.loc["products", "file_name"] == "products.csv"
    assert "list_price" in spec.loc["products", "must_parse_as_number"]
    assert "date" in spec.loc["transactions", "must_parse_as_date"]


def test_a_workbook_profile_describes_csv_names_too():
    """A folder of CSVs named after the sheets is a valid source for a workbook profile."""
    spec = describe(DAIRY).set_index("table")
    assert spec.loc["financial_plan", "workbook_sheet"] == "24_Financial_Plan"
    assert spec.loc["financial_plan", "file_name"] == "24_Financial_Plan.csv"


def test_a_workbook_profile_can_read_a_directory_of_csvs(snapshot, tmp_path):
    """The same loader serves both, which is the point of `Profile.file_for`."""
    from pricing.mapping.fixture import FixtureSpec, build_tables
    from pricing.mapping.r2rml import SourceMapping, find_mapping

    mapping_path = find_mapping(Path(__file__).resolve().parents[2])
    if mapping_path is None:
        pytest.skip("no axiom-source-mapping*.json alongside the repo")

    out = tmp_path / "dairy_csv"
    out.mkdir()
    tables = build_tables(
        FixtureSpec(mapping=SourceMapping.load(mapping_path), out_path=out / "unused.xlsx")
    )
    for sheet, df in tables.items():
        df.to_csv(out / f"{sheet}.csv", index=False)

    verdict = check(out, DAIRY)
    assert verdict.fit is Fit.EXACT, str(verdict)

    source = CsvGraphSource(out, profile=DAIRY)
    plan = source.table("financial_plan")
    assert "Planned_Revenue_SAR" in plan.columns
    assert pd.api.types.is_numeric_dtype(plan["Planned_Revenue_SAR"]), (
        "the profile's semantic types must survive a CSV round-trip"
    )


def test_the_csv_source_names_the_profile_when_a_table_is_unknown(snapshot):
    """An error a reader can act on beats a KeyError with a bare name in it."""
    source = CsvGraphSource(snapshot, profile=B2B)
    with pytest.raises(KeyError, match="b2b_industrial"):
        source.table("financial_plan")


def test_provenance_records_which_profile_read_the_snapshot(snapshot):
    """The same bytes read under two profiles are two different reads."""
    info = CsvGraphSource(snapshot, profile=B2B).snapshot_info()
    assert info["profile"] == "b2b_industrial"
    assert info["content_sha256"]
