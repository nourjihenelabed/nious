"""Entry-point validation (D1): decide whether a snapshot is fit to compute on."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .source import GraphSource, Source


class Severity(str, Enum):
    """Blocking findings stop the run; warnings travel into confidence reasons."""

    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One validation finding, with its blast radius."""

    severity: Severity
    check: str
    detail: str
    n_affected: int = 0

    def __str__(self) -> str:
        """One printable line."""
        n = f" ({self.n_affected:,} records)" if self.n_affected else ""
        label = (
            "STOPS THE RUN" if self.severity is Severity.BLOCKING
            else self.severity.value.upper()
        )
        return f"[{label}] {self.check}: {self.detail}{n}"


@dataclass
class ValidationReport:
    """Every finding from one validation pass."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        """Findings that make the snapshot unfit to compute on."""
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def warnings(self) -> list[Finding]:
        """Findings that degrade the result without invalidating it."""
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found."""
        return not self.blocking

    def reasons(self) -> tuple[str, ...]:
        """Warning text, shaped for Confidence.reasons on the result object."""
        return tuple(f"{f.check}: {f.detail}" for f in self.warnings)

    def __str__(self) -> str:
        """The whole report as printable text."""
        if not self.findings:
            return "Snapshot validated: no findings."
        head = (
            f"Data checks: {len(self.blocking)} problem(s) that stop the run, "
            f"{len(self.warnings)} warning(s)"
        )
        return "\n".join([head, *(f"  {f}" for f in self.findings)])


class SnapshotRejected(RuntimeError):
    """Raised when a snapshot is too broken to compute on."""

    def __init__(self, report: ValidationReport):
        """Carry the report that caused the refusal."""
        self.report = report
        super().__init__("Refusing to run on this snapshot.\n" + str(report))


def validate(src: Source, profile=None) -> ValidationReport:
    """Run every entry-point check and return the report, without raising."""
    from ..profiles import get_profile

    profile = profile or get_profile()
    report = ValidationReport()
    add = report.findings.append

    for name in profile.tables:
        try:
            df = src.table(name)
        except Exception as exc:
            add(Finding(Severity.BLOCKING, "table available", f"{name} unreadable: {exc}"))
            continue
        if df.empty:
            add(Finding(Severity.BLOCKING, "table non-empty", f"{name} has no rows"))

    if report.blocking:
        return report

    checker = getattr(profile, "validate_source", None)
    if checker is not None:
        checker(src, report)
    return report


def _b2b_checks(src: GraphSource, report: ValidationReport) -> ValidationReport:
    """Entry-point checks for the `b2b_industrial` schema."""
    import pandas as pd

    add = report.findings.append
    txn = src.transactions()
    lines = src.transaction_lines()
    tdisc = src.transaction_discounts()
    prods = src.products()

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

    future = txn[txn["date"] > pd.Timestamp.now()]
    if len(future):
        add(Finding(Severity.WARNING, "semantic type", "transactions dated in the future", len(future)))

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

    line_sum = lines.groupby("transaction_id")["line_total"].sum()
    header = txn.set_index("transaction_id")["gross_amount"]
    diff = (line_sum.reindex(header.index) - header).abs()
    n_bad = int((diff > 0.01).sum())
    if n_bad:
        add(
            Finding(
                Severity.BLOCKING,
                "header/line agreement",
                "transaction gross_amount disagrees with the sum of its line totals",
                n_bad,
            )
        )

    return report


def require_valid(src: Source, profile=None) -> ValidationReport:
    """Validate and refuse to continue on blocking findings."""
    report = validate(src, profile)
    if not report.ok:
        raise SnapshotRejected(report)
    return report
