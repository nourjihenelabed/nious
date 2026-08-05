"""
Entry-point validation for the graph-access layer (D1).

Brief section 7.4, "stage gates and audit", requires the module to
*validate inputs at the entry point and refuse to run on incomplete data*.

This is that gate. It runs once, before any pricing computation, and answers a
single question: is this snapshot fit to compute on at all?

Two levels, deliberately separated:

    BLOCKING  -- the snapshot is structurally broken. Refuse to run. Examples:
                 a required table is missing, a transaction has no customer,
                 line items reference products that do not exist.
    WARNING   -- the snapshot is usable but degraded. Run, and carry the
                 warnings into the result's confidence reasons. Examples:
                 some products lack a unit cost, some discount edges have no
                 amount.

The distinction matters. Refusing to run on any imperfection would make the
module useless on real data; running silently on broken data would make it
dangerous. The blocking set is small and is about *structure*, never about
completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .. import config
from .source import GraphSource


class Severity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    check: str
    detail: str
    n_affected: int = 0

    def __str__(self) -> str:
        n = f" ({self.n_affected} affected)" if self.n_affected else ""
        return f"[{self.severity.value.upper()}] {self.check}: {self.detail}{n}"


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def reasons(self) -> tuple[str, ...]:
        """Warning text, shaped for Confidence.reasons on the result object."""
        return tuple(f"{f.check}: {f.detail}" for f in self.warnings)

    def __str__(self) -> str:
        if not self.findings:
            return "Snapshot validated: no findings."
        head = f"Snapshot validation: {len(self.blocking)} blocking, {len(self.warnings)} warnings"
        return "\n".join([head, *(f"  {f}" for f in self.findings)])


class SnapshotRejected(RuntimeError):
    """Raised when a snapshot is too broken to compute on."""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("Refusing to run on this snapshot.\n" + str(report))


# --------------------------------------------------------------------------
def validate(src: GraphSource) -> ValidationReport:
    """Run every entry-point check and return the report (does not raise)."""
    report = ValidationReport()
    add = report.findings.append

    # -- 1. All tables present and non-empty ------------------------------
    for name in config.CSV_FILES:
        try:
            df = src.table(name)
        except Exception as exc:  # missing file, bad query, ...
            add(Finding(Severity.BLOCKING, "table available", f"{name} unreadable: {exc}"))
            continue
        if df.empty:
            add(Finding(Severity.BLOCKING, "table non-empty", f"{name} has no rows"))

    if report.blocking:
        return report  # nothing further is meaningful

    txn = src.transactions()
    lines = src.transaction_lines()
    tdisc = src.transaction_discounts()
    prods = src.products()

    # -- 2. Structural integrity: no dangling references ------------------
    checks = [
        ("transaction -> customer", txn["customer_id"], set(src.customers()["customer_id"])),
        ("transaction -> channel", txn["channel_id"], set(src.channels()["channel_id"])),
        ("customer -> region", src.customers()["region_id"], set(src.regions()["region_id"])),
        ("line -> transaction", lines["transaction_id"], set(txn["transaction_id"])),
        ("line -> product", lines["product_id"], set(prods["product_id"])),
        ("discount edge -> transaction", tdisc["transaction_id"], set(txn["transaction_id"])),
        ("discount edge -> discount", tdisc["discount_id"], set(src.discounts()["discount_id"])),
    ]
    for label, series, valid in checks:
        missing = series[series.notna() & ~series.isin(valid)]
        if len(missing):
            add(
                Finding(
                    Severity.BLOCKING,
                    "referential integrity",
                    f"{label} has unresolved references, e.g. {list(missing.unique()[:3])}",
                    len(missing),
                )
            )

    # -- 3. Identity: keys must be unique ---------------------------------
    for label, series in (
        ("transaction_id", txn["transaction_id"]),
        ("customer_id", src.customers()["customer_id"]),
        ("product_id", prods["product_id"]),
    ):
        dupes = series[series.duplicated()]
        if len(dupes):
            add(
                Finding(
                    Severity.BLOCKING,
                    "unique keys",
                    f"duplicate {label}: {list(dupes.unique()[:3])}",
                    len(dupes),
                )
            )

    # -- 4. A transaction with no lines has no pricing basis --------------
    no_lines = set(txn["transaction_id"]) - set(lines["transaction_id"])
    if no_lines:
        add(
            Finding(
                Severity.BLOCKING,
                "pricing basis",
                "transactions with no CONTAINS line cannot be decomposed",
                len(no_lines),
            )
        )

    # -- 5. Semantic types (Brief 5.3) ------------------------------------
    # Negative money is a contradiction, not a data gap -> blocking.
    for label, series in (
        ("product.list_price", prods["list_price"]),
        ("product.unit_cost", prods["unit_cost"]),
        ("line.unit_price", lines["unit_price"]),
        ("line.line_total", lines["line_total"]),
        ("transaction.gross_amount", txn["gross_amount"]),
    ):
        neg = series[series < 0]
        if len(neg):
            add(Finding(Severity.BLOCKING, "semantic type", f"{label} has negative values", len(neg)))

    neg_qty = lines[lines["quantity"] < 0]
    if len(neg_qty):
        add(Finding(Severity.BLOCKING, "semantic type", "negative line quantity", len(neg_qty)))

    future = txn[txn["date"] > __import__("pandas").Timestamp.now()]
    if len(future):
        add(Finding(Severity.WARNING, "semantic type", "transactions dated in the future", len(future)))

    # -- 6. Completeness -> warnings, carried into confidence -------------
    n_no_list = int(prods["list_price"].isna().sum())
    if n_no_list:
        affected = lines[lines["product_id"].isin(prods.loc[prods["list_price"].isna(), "product_id"])]
        add(
            Finding(
                Severity.WARNING,
                "list price coverage",
                f"{n_no_list} product(s) have no list_price; "
                f"{affected['transaction_id'].nunique()} transactions cannot report price realisation",
                affected["transaction_id"].nunique(),
            )
        )

    n_no_cost = int(prods["unit_cost"].isna().sum())
    if n_no_cost:
        affected = lines[lines["product_id"].isin(prods.loc[prods["unit_cost"].isna(), "product_id"])]
        add(
            Finding(
                Severity.WARNING,
                "cost coverage",
                f"{n_no_cost} product(s) have no unit_cost; "
                f"{affected['transaction_id'].nunique()} transactions cannot report pocket margin",
                affected["transaction_id"].nunique(),
            )
        )

    # -- 6b. Multiple currencies with no conversion rate ------------------
    # A warning rather than a blocker: transaction-grain and region-grain
    # results are still valid. It is only aggregates that SPAN currencies that
    # are meaningless, and the engine refuses to publish those individually
    # (engine/result.py). Flagging it here means it appears in every run log.
    currencies = src.regions()["currency"].dropna().unique()
    if len(currencies) > 1:
        has_fx = any(
            "rate" in c.lower() or "fx" in c.lower()
            for c in list(src.regions().columns) + list(txn.columns)
        )
        if not has_fx:
            add(
                Finding(
                    Severity.WARNING,
                    "currency",
                    f"{len(currencies)} currencies ({', '.join(sorted(currencies))}) and no FX rate "
                    "in the graph -- aggregates spanning currencies cannot be published; "
                    "include `currency` in the grain",
                    len(currencies),
                )
            )

    n_unknown_amt = int(tdisc["discount_amount_applied"].isna().sum())
    if n_unknown_amt:
        add(
            Finding(
                Severity.WARNING,
                "discount amounts",
                "discount applied with unknown amount -- leakage understated on those deals",
                n_unknown_amt,
            )
        )

    # -- 7. Header vs lines must agree ------------------------------------
    line_sum = lines.groupby("transaction_id")["line_total"].sum()
    header = txn.set_index("transaction_id")["gross_amount"]
    diff = (line_sum.reindex(header.index) - header).abs()
    n_bad = int((diff > 0.01).sum())
    if n_bad:
        # The header and the edges disagreeing means we do not know which is
        # true. That is structural, not a gap.
        add(
            Finding(
                Severity.BLOCKING,
                "header/line agreement",
                "transaction gross_amount disagrees with the sum of its line totals",
                n_bad,
            )
        )

    return report


def require_valid(src: GraphSource) -> ValidationReport:
    """
    Validate and refuse to continue on blocking findings.

    Call this at the top of every engine entry point. `SnapshotRejected` is a
    loud, specific failure -- much better than a quiet wrong number.
    """
    report = validate(src)
    if not report.ok:
        raise SnapshotRejected(report)
    return report
