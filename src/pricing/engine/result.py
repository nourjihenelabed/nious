"""
Translate engine internals into the Track B contract.

This is the only module allowed to build `contracts.EngineResult` objects. The
separation is deliberate: internally the engine speaks pandas, because that is
what the maths wants; externally it speaks typed records with evidence and
confidence attached, because that is what a governance product needs.

Keeping the translation in one file means the contract can be frozen (Week 7)
without freezing how the waterfall is computed.
"""

from __future__ import annotations

import pandas as pd

from ..contracts import schema as C
from .aggregation import aggregate, resolve
from .waterfall import Waterfall


def _money(value, currency: str = "EUR", reason: str = "input missing") -> C.Money:
    """A number becomes Money; a NaN becomes an explicit UNAVAILABLE."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return C.Money.unavailable(reason, currency)
    return C.Money(round(float(value), 2), currency)


def _confidence_for(row: pd.Series, reasons: tuple[str, ...] = ()) -> C.Confidence:
    """
    Confidence for a waterfall row.

    Deliberately simple and explainable in one sentence: confidence IS coverage,
    lightly penalised when the group is thin. A clever opaque score would be
    worse here -- Track B has to justify this number to a reader.

    The real Pricing Data Readiness Score (D3, Week 4) will supersede this.
    """
    coverage = row.get("coverage_pct")
    coverage = 0.0 if pd.isna(coverage) else float(coverage) / 100.0

    n = int(row.get("n_transactions_certified", 0) or 0)
    # Thin evidence caps confidence: 1 deal cannot be "high confidence" however
    # complete its data is.
    thinness = min(1.0, n / 10.0)
    score = coverage * (0.5 + 0.5 * thinness)

    why = list(reasons)
    if coverage < 1.0:
        excluded = int(row.get("n_transactions", 0)) - n
        why.append(f"{excluded} of {int(row.get('n_transactions', 0))} transactions not fully decomposable")
    if n < 10:
        why.append(f"only {n} certified transaction(s) in this group")
    return C.Confidence.from_score(score, tuple(why))


MIXED_CURRENCY_NOTE = (
    "aggregate spans {n} currencies ({codes}) and the graph carries no FX rate; "
    "summing them would produce a number with no unit"
)


def _is_mixed_currency(row: pd.Series) -> bool:
    return int(row.get("n_currencies", 1) or 1) > 1


def _status_for(row: pd.Series) -> C.ResultStatus:
    # A cross-currency sum is not a number we can defend. Abstain, and say why.
    # The caller's fix is to include `currency` in the grain.
    if _is_mixed_currency(row):
        return C.ResultStatus.ABSTAINED
    declared = row.get("status")
    if declared == "unavailable":
        return C.ResultStatus.UNAVAILABLE
    if declared == "abstained":
        return C.ResultStatus.ABSTAINED
    coverage = row.get("coverage_pct")
    if pd.isna(coverage):
        return C.ResultStatus.UNAVAILABLE
    return C.ResultStatus.COMPLETE if coverage >= 100 else C.ResultStatus.PARTIAL


def _evidence_for(facts: pd.DataFrame, cols: list[str], key: dict, max_ids: int = 200) -> C.Evidence:
    """
    The graph records this row was computed from.

    Capped at `max_ids`: Track B needs enough to cite, not the entire book. The
    full set is always recoverable by re-running the same groupby, and the cap
    keeps a result payload from becoming unusable at real data volumes.
    """
    mask = pd.Series(True, index=facts.index)
    for col in cols:
        mask &= facts[col] == key[col]
    sub = facts[mask & facts["certified"]]
    return C.Evidence(
        transaction_ids=tuple(sub["transaction_id"].unique()[:max_ids]),
        customer_ids=tuple(sub["customer_id"].dropna().unique()[:max_ids]),
        product_ids=tuple(sub["product_id"].dropna().unique()[:max_ids]),
    )


def waterfall_records(
    w: Waterfall,
    dimension: str | list[str],
    min_transactions: int = 0,
    with_evidence: bool = True,
) -> tuple[C.WaterfallRecord, ...]:
    """Aggregate to `dimension` (a name or a list of names) and emit records."""
    cols = resolve(dimension)
    grain = dimension if isinstance(dimension, str) else "x".join(dimension)
    agg = aggregate(w, dimension, min_transactions=min_transactions)

    records = []
    for _, row in agg.iterrows():
        key = {c: row[c] for c in cols}

        # Currency of the group. Unambiguous only when the group holds one.
        codes = str(row.get("currencies", "") or "")
        mixed = _is_mixed_currency(row)
        currency = "MIXED" if mixed else (codes or "EUR")
        money_reason = (
            MIXED_CURRENCY_NOTE.format(n=int(row.get("n_currencies", 0)), codes=codes)
            if mixed
            else "input missing"
        )

        reason_texts = tuple(
            sorted(
                set(
                    w.facts.loc[
                        ~w.facts["certified"] & (w.facts[cols[0]] == key[cols[0]]),
                        "uncertified_reason",
                    ].dropna()
                )
                - {""}
            )
        ) if cols[0] in w.facts else ()

        def m(step: str) -> C.Money:
            # A cross-currency group publishes nothing: the amount exists in the
            # dataframe for inspection, but it is not a defensible figure, so it
            # does not cross the contract boundary.
            if mixed:
                return C.Money.unavailable(money_reason, currency)
            return _money(row.get(step), currency, money_reason)

        if mixed:
            reason_texts = (*reason_texts, money_reason)

        records.append(
            C.WaterfallRecord(
                grain=grain,
                key=key,
                list_value=m("list_value"),
                negotiated_off_list=m("negotiated_off_list"),
                gross_value=m("gross_value"),
                on_invoice_discount=m("on_invoice_discount"),
                invoice_price=m("invoice_price"),
                off_invoice_leakage=m("off_invoice_leakage"),
                pocket_price=m("pocket_price"),
                cogs=m("cogs"),
                pocket_margin=m("pocket_margin"),
                # Ratios survive a currency mix only if every leg shares a unit,
                # which is exactly what mixed means it does not. Suppress them too.
                pocket_margin_pct=None if (mixed or pd.isna(row.get("pocket_margin_pct")))
                else round(float(row["pocket_margin_pct"]), 2),
                price_realisation_pct=None if (mixed or pd.isna(row.get("price_realisation_pct")))
                else round(float(row["price_realisation_pct"]), 2),
                status=_status_for(row),
                confidence=C.Confidence.from_score(0.0, reason_texts) if mixed
                else _confidence_for(row, reason_texts),
                evidence=_evidence_for(w.facts, cols, key) if with_evidence else C.Evidence(),
                n_transactions=int(row.get("n_transactions", 0) or 0),
                n_certified=int(row.get("n_transactions_certified", 0) or 0),
            )
        )
    return tuple(records)


def build(
    w: Waterfall,
    grains: tuple[str | list[str], ...] = (["segment", "currency"],),
    min_transactions: int = 0,
    mlflow_run_id: str | None = None,
) -> C.EngineResult:
    """Assemble the full result envelope Track B receives."""
    snap = w.snapshot
    provenance = C.Provenance(
        snapshot_backend=snap.get("backend", "unknown"),
        snapshot_location=snap.get("location", "unknown"),
        snapshot_hash=snap.get("content_sha256"),
        mlflow_run_id=mlflow_run_id,
    )

    records: list[C.WaterfallRecord] = []
    for grain in grains:
        records.extend(waterfall_records(w, grain, min_transactions=min_transactions))

    dates = w.facts["date"].dropna()
    period = (dates.min().date(), dates.max().date()) if len(dates) else (None, None)

    return C.EngineResult(
        provenance=provenance,
        waterfalls=tuple(records),
        data_quality_flags=tuple(w.flags.to_dict(orient="records")),
        period=period,
    )
