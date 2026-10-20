"""Translate engine internals (pandas) into the typed Track B contract."""

from __future__ import annotations

import math

import pandas as pd

from ..config import THIN_EVIDENCE_DEALS
from ..contracts import schema as C
from .aggregation import aggregate
from .waterfall import Waterfall


def _finite(value) -> float | None:
    """Coerce to a finite float, or None. NaN and +/-inf are both 'no number'."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value, currency: str = "EUR", reason: str = "input missing") -> C.Money:
    """A number becomes Money; anything not finite becomes an explicit UNAVAILABLE."""
    number = _finite(value)
    if number is None:
        return C.Money.unavailable(reason, currency)
    return C.Money(round(number, 2), currency)


def _pct(value) -> float | None:
    """A ratio becomes a rounded float, or None when it does not exist."""
    number = _finite(value)
    return None if number is None else round(number, 2)


def _confidence_for(row: pd.Series, reasons: tuple[str, ...] = ()) -> C.Confidence:
    """Confidence for a waterfall row: coverage, penalised when evidence is thin."""
    coverage = row.get("coverage_pct")
    coverage = 0.0 if pd.isna(coverage) else float(coverage) / 100.0

    n = int(row.get("n_transactions_certified", 0) or 0)
    thinness = min(1.0, n / float(THIN_EVIDENCE_DEALS))
    score = coverage * (0.5 + 0.5 * thinness)

    why = list(reasons)
    if coverage < 1.0:
        excluded = int(row.get("n_transactions", 0)) - n
        why.append(f"{excluded} of {int(row.get('n_transactions', 0))} transactions not fully decomposable")
    if n < THIN_EVIDENCE_DEALS:
        why.append(f"only {n} certified transaction(s) in this group")
    return C.Confidence.from_score(score, tuple(why))


#: The nine steps `contracts.WaterfallRecord` has fields for.
CANONICAL_STEPS: tuple[str, ...] = (
    "list_value", "negotiated_off_list", "gross_value", "on_invoice_discount",
    "invoice_price", "off_invoice_leakage", "pocket_price", "cogs", "pocket_margin",
)

MIXED_CURRENCY_NOTE = (
    "aggregate spans {n} currencies ({codes}) and the graph carries no FX rate; "
    "summing them would produce a number with no unit"
)


def _step_money(
    step: str,
    row: pd.Series,
    profile,
    absent: dict[str, str],
    currency: str,
    mixed: bool,
    money_reason: str,
) -> C.Money:
    """One canonical step as Money, or an UNAVAILABLE that says why."""
    if mixed:
        return C.Money.unavailable(money_reason, currency)
    if not profile.has(step):
        return C.Money.unavailable(
            absent.get(step, f"{step} is not part of the {profile.name} waterfall"),
            currency,
        )
    if step in absent and pd.isna(row.get(step)):
        return C.Money.unavailable(absent[step], currency)
    return _money(row.get(step), currency, money_reason)


def _is_mixed_currency(row: pd.Series) -> bool:
    """True when a group spans more than one currency."""
    return int(row.get("n_currencies", 1) or 1) > 1


def _status_for(row: pd.Series) -> C.ResultStatus:
    """Decide COMPLETE / PARTIAL / UNAVAILABLE / ABSTAINED for one group."""
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


def _evidence_for(
    w: Waterfall, cols: list[str], key: dict, max_ids: int = 200
) -> C.Evidence:
    """The certified records this row was computed from, capped for payload size."""
    facts = w.facts
    mask = pd.Series(True, index=facts.index)
    for col in cols:
        if col in facts.columns:
            mask &= facts[col] == key[col]
    sub = facts[mask & facts["certified"]]

    def ids(column: str) -> tuple[str, ...]:
        """Distinct non-null ids from one column, or nothing when it is absent."""
        if column not in sub.columns:
            return ()
        return tuple(sub[column].dropna().unique()[:max_ids])

    return C.Evidence(
        transaction_ids=ids(w.profile.key),
        customer_ids=ids("customer_id"),
        product_ids=ids("product_id"),
    )


def waterfall_records(
    w: Waterfall,
    dimension: str | list[str],
    min_transactions: int = 0,
    with_evidence: bool = True,
) -> tuple[C.WaterfallRecord, ...]:
    """Aggregate to `dimension` and emit one typed contract record per group."""
    profile = w.profile
    cols = profile.resolve(dimension)
    grain = dimension if isinstance(dimension, str) else "x".join(dimension)
    agg = aggregate(w, dimension, min_transactions=min_transactions)
    absent = dict(profile.absent_steps)
    for step in profile.step_names:
        if step in agg.columns and agg[step].isna().all() and step not in absent:
            note = str(agg["grain_note"].iloc[0]) if "grain_note" in agg.columns else ""
            if note:
                absent.setdefault(step, note)

    records = []
    for _, row in agg.iterrows():
        key = {c: row[c] for c in cols}

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
        if absent:
            reason_texts = (*reason_texts, *sorted(set(absent.values())))

        if mixed:
            reason_texts = (*reason_texts, money_reason)

        money = {
            step: _step_money(step, row, profile, absent, currency, mixed, money_reason)
            for step in CANONICAL_STEPS
        }

        records.append(
            C.WaterfallRecord(
                grain=grain,
                key=key,
                list_value=money["list_value"],
                negotiated_off_list=money["negotiated_off_list"],
                gross_value=money["gross_value"],
                on_invoice_discount=money["on_invoice_discount"],
                invoice_price=money["invoice_price"],
                off_invoice_leakage=money["off_invoice_leakage"],
                pocket_price=money["pocket_price"],
                cogs=money["cogs"],
                pocket_margin=money["pocket_margin"],
                pocket_margin_pct=None if mixed else _pct(row.get("pocket_margin_pct")),
                price_realisation_pct=None if mixed else _pct(row.get("price_realisation_pct")),
                status=_status_for(row),
                confidence=C.Confidence.from_score(0.0, reason_texts) if mixed
                else _confidence_for(row, reason_texts),
                evidence=_evidence_for(w, cols, key) if with_evidence else C.Evidence(),
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

    date_col = w.profile.date_column
    dates = w.facts[date_col].dropna() if date_col and date_col in w.facts else pd.Series(dtype="datetime64[ns]")
    period = (dates.min().date(), dates.max().date()) if len(dates) else (None, None)

    return C.EngineResult(
        provenance=provenance,
        waterfalls=tuple(records),
        data_quality_flags=tuple(w.flags.to_dict(orient="records")),
        period=period,
    )
