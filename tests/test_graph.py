"""Graph-access tests (D1): read-only guarantee, integrity, and the entry gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.graph import CsvGraphSource, cypher, validation  # noqa: E402
from pricing.quality import profiling  # noqa: E402


@pytest.fixture(scope="module")
def src() -> CsvGraphSource:
    """The synthetic snapshot the module runs against."""
    return CsvGraphSource()


def test_no_write_clauses_in_cypher():
    """The graph is read-only by contract. Prove it mechanically, not by promise."""
    text = Path(cypher.__file__).read_text(encoding="utf-8").upper()
    body = text.split('"""', 2)[-1]
    for clause in ("CREATE ", "MERGE ", "DELETE ", " SET ", "DETACH", "DROP "):
        assert clause not in body, f"write clause {clause!r} found in cypher.py"


def test_every_table_has_a_query():
    """CSV and Neo4j backends must expose exactly the same set of tables."""
    from pricing import config

    assert set(cypher.QUERIES) == set(config.CSV_FILES)


def test_all_tables_load(src):
    """All eight tables read, and none is empty."""
    tables = src.tables()
    assert len(tables) == 8
    for name, df in tables.items():
        assert not df.empty, f"{name} is empty"


def test_no_dangling_edges(src):
    """Every foreign key resolves."""
    ri = profiling.referential_integrity(src)
    bad = ri[~ri["ok"]]
    assert bad.empty, f"unresolved references:\n{bad}"


def test_semantic_types(src):
    """Money positive, quantities non-negative, no implausible dates."""
    num = profiling.numeric_profile(src)
    money = num[num["column"].isin(
        ["list_price", "unit_cost", "unit_price", "line_total", "gross_amount"]
    )]
    assert (money["n_negative"] == 0).all(), "negative monetary values found"

    txn = src.transactions()
    assert (txn["quantity"] >= 0).all()
    assert txn["date"].max() <= pd.Timestamp("2026-12-31")


def test_snapshot_is_reproducible(src):
    """Same snapshot -> same hash -> same results."""
    assert (
        CsvGraphSource().snapshot_info()["content_sha256"]
        == CsvGraphSource().snapshot_info()["content_sha256"]
    )


def test_snapshot_passes_validation(src):
    """The current snapshot is degraded but structurally sound, so it must run."""
    report = validation.validate(src)
    assert report.ok, f"unexpected blocking findings:\n{report}"


def test_validation_reports_completeness_as_warnings(src):
    """Missing product fields are warnings, not blockers -- and they are named."""
    report = validation.validate(src)
    checks = {f.check for f in report.warnings}
    assert "list price coverage" in checks
    assert "cost coverage" in checks
    assert report.reasons(), "warnings must produce reasons for the confidence score"


def test_validation_blocks_a_broken_snapshot(src, tmp_path):
    """A dangling customer reference must stop the run."""
    from pricing import config

    for fname in config.CSV_FILES.values():
        (tmp_path / fname).write_bytes((Path(src.data_dir) / fname).read_bytes())

    txn = pd.read_csv(tmp_path / "transactions.csv")
    txn.loc[0, "customer_id"] = "CUS-DOES-NOT-EXIST"
    txn.to_csv(tmp_path / "transactions.csv", index=False)

    broken = CsvGraphSource(tmp_path)
    report = validation.validate(broken)
    assert not report.ok
    assert any("referential integrity" in f.check for f in report.blocking)

    with pytest.raises(validation.SnapshotRejected):
        validation.require_valid(broken)


def test_net_amount_semantics_are_as_observed(src):
    """net_amount nets OFF-invoice discounts here, not on-invoice. Pinned deliberately."""
    rec = profiling.amount_reconciliation(src).set_index("hypothesis")
    assert rec.loc["H1: gross == sum(line_total)", "holds"]
    assert rec.loc["H4: net == gross - off-invoice", "holds"]
    assert not rec.loc["H2: net == gross - on-invoice", "holds"]
