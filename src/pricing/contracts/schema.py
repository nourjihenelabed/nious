"""
The engine -> narrative result contract (D8, draft v0.2).

THIS FILE IS THE BOUNDARY BETWEEN TRACK A AND TRACK B.

Track A (this codebase) computes numbers and emits the objects defined here.
Track B consumes them, retrieves supporting evidence from the graph, and writes
a cited narrative. The rule from the Brief (section 7.3):

    The narrative layer explains only numbers the engine computed.
    It never invents or recalculates figures.

Everything in this file is designed to make that rule enforceable rather than
merely stated:

1. `evidence_ids` on every record. Track B cites *these* records and no others.
   A result that cannot name its sources is not shippable.
2. `Confidence` is a value, not an adjective. It carries a score, a band, and
   the reasons behind it, so the narrative can say WHY confidence is low.
3. `status` distinguishes COMPLETE from PARTIAL from UNAVAILABLE. A field that
   is None because data was missing is never confusable with a field that is
   legitimately zero.
4. `Provenance` on the envelope. Every result names the snapshot hash and code
   version it came from, so any number can be regenerated and audited.

STATUS: draft. Agreed in principle with Mohamed in Week 2, FROZEN in Week 7
(onboarding plan section 8). Additive changes only until then; any breaking
change needs both tracks to sign off and a bump of CONTRACT_VERSION.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timezone
from enum import Enum
from typing import Any

CONTRACT_VERSION = "0.2.0-draft"


# ==========================================================================
# Shared value types
# ==========================================================================
class ResultStatus(str, Enum):
    """Why a number is (or is not) present. Never guess from a null."""

    COMPLETE = "complete"        # every input present; number is certified
    PARTIAL = "partial"          # computed, but some inputs missing -> use with care
    UNAVAILABLE = "unavailable"  # cannot be computed; do NOT substitute zero
    ABSTAINED = "abstained"      # computable, but evidence too thin to publish


class ConfidenceBand(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class Confidence:
    """
    Quantified confidence, plus the reasons for it.

    The reasons matter as much as the score: Track B's narrative is required to
    explain low confidence, and it can only do that if we tell it why.
    """

    score: float                      # 0.0 - 1.0
    band: ConfidenceBand
    reasons: tuple[str, ...] = ()     # e.g. "3 of 12 lines missing unit_cost"

    @staticmethod
    def from_score(score: float, reasons: tuple[str, ...] = ()) -> "Confidence":
        score = max(0.0, min(1.0, float(score)))
        if score >= 0.80:
            band = ConfidenceBand.HIGH
        elif score >= 0.50:
            band = ConfidenceBand.MEDIUM
        elif score > 0.0:
            band = ConfidenceBand.LOW
        else:
            band = ConfidenceBand.NONE
        return Confidence(round(score, 4), band, tuple(reasons))


@dataclass(frozen=True)
class Money:
    """
    A monetary amount that can be absent for a stated reason.

    Using this instead of a bare float is the type-level version of the rule
    'missing is not zero': you cannot read `.amount` without also seeing
    `.status`.
    """

    amount: float | None
    currency: str = "EUR"
    status: ResultStatus = ResultStatus.COMPLETE
    note: str | None = None

    @staticmethod
    def unavailable(reason: str, currency: str = "EUR") -> "Money":
        return Money(None, currency, ResultStatus.UNAVAILABLE, reason)

    def __bool__(self) -> bool:  # guards against `if money:` meaning `if amount`
        return self.amount is not None


@dataclass(frozen=True)
class Evidence:
    """
    Pointer to the graph records a number was computed from.

    Track B resolves these back to nodes/edges to build citations. Without this,
    a narrative cannot be grounded, and an ungrounded narrative is exactly the
    failure mode the Brief forbids.
    """

    transaction_ids: tuple[str, ...] = ()
    customer_ids: tuple[str, ...] = ()
    product_ids: tuple[str, ...] = ()
    discount_ids: tuple[str, ...] = ()

    def count(self) -> int:
        return len(self.transaction_ids)

    def is_empty(self) -> bool:
        return not any(
            (self.transaction_ids, self.customer_ids, self.product_ids, self.discount_ids)
        )


# ==========================================================================
# Result records (Brief section 6)
# ==========================================================================
@dataclass(frozen=True)
class WaterfallRecord:
    """
    One price waterfall, at whatever grain it was computed
    (transaction, customer, segment, ...).

    `grain` and `key` say what this row IS, so a narrative never has to infer it
    from context: grain="segment", key={"segment": "Enterprise"}.
    """

    grain: str
    key: dict[str, Any]

    list_value: Money
    negotiated_off_list: Money
    gross_value: Money
    on_invoice_discount: Money
    invoice_price: Money
    off_invoice_leakage: Money
    pocket_price: Money
    cogs: Money
    pocket_margin: Money

    pocket_margin_pct: float | None
    price_realisation_pct: float | None

    status: ResultStatus
    confidence: Confidence
    evidence: Evidence

    # How much of the underlying population this row actually covers.
    n_transactions: int = 0
    n_certified: int = 0

    @property
    def coverage_pct(self) -> float | None:
        if not self.n_transactions:
            return None
        return round(100.0 * self.n_certified / self.n_transactions, 2)


@dataclass(frozen=True)
class SegmentAssignment:
    """Customer -> segment, with the behaviour that justified it. (D4, Week 5.)"""

    customer_id: str
    segment_label: str
    segment_score: float | None
    price_sensitivity_index: float | None
    volume_tier: str | None
    margin_contribution: Money
    status: ResultStatus
    confidence: Confidence
    evidence: Evidence
    distinguishing_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceRange:
    low: float
    mid: float
    high: float

    def __post_init__(self) -> None:
        if not (self.low <= self.mid <= self.high):
            raise ValueError(f"price range not ordered: {self.low}/{self.mid}/{self.high}")


@dataclass(frozen=True)
class PriceRecommendation:
    """A recommended price for a customer+product context. (D6, Week 6.)"""

    customer_id: str
    product_id: str
    price_range: PriceRange | None
    expected_margin: Money
    acceptance_probability: float | None
    comparable_deals_count: int
    status: ResultStatus
    confidence: Confidence
    evidence: Evidence
    constraints_applied: tuple[str, ...] = ()   # "floor=cost*1.05", "ceiling=P90 corridor"


@dataclass(frozen=True)
class ElasticityEstimate:
    """Elasticity with mandatory uncertainty. (D5, Week 5.)"""

    scope: dict[str, Any]                # e.g. {"segment": "SMB", "category": "Drives"}
    beta: float | None                   # the elasticity itself
    credible_interval: tuple[float, float] | None
    n_observations: int
    shrunk_toward_prior: bool
    fit_for_optimisation: bool           # False if interval crosses 0 or sign implausible
    status: ResultStatus
    confidence: Confidence
    evidence: Evidence


# ==========================================================================
# Envelope
# ==========================================================================
@dataclass(frozen=True)
class Provenance:
    """Where a result came from. Without this a result is not auditable."""

    snapshot_backend: str
    snapshot_location: str
    snapshot_hash: str | None
    contract_version: str = CONTRACT_VERSION
    engine_version: str = "0.2.0"
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mlflow_run_id: str | None = None


@dataclass(frozen=True)
class EngineResult:
    """
    The single object Track B receives. One run of the engine -> one of these.

    Deliberately a container of typed records rather than a free-form dict:
    Track B should be able to write `result.waterfalls[0].pocket_price.amount`
    and get a compile-time-ish guarantee, not fish through JSON keys.
    """

    provenance: Provenance
    waterfalls: tuple[WaterfallRecord, ...] = ()
    segments: tuple[SegmentAssignment, ...] = ()
    recommendations: tuple[PriceRecommendation, ...] = ()
    elasticities: tuple[ElasticityEstimate, ...] = ()
    data_quality_flags: tuple[dict[str, Any], ...] = ()
    period: tuple[Date | None, Date | None] = (None, None)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self, dict_factory=_dict_factory)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=_json_default)


def _dict_factory(items):
    return {k: v for k, v in items}


def _json_default(o):
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (datetime, Date)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


# ==========================================================================
# Contract self-check
# ==========================================================================
def validate(result: EngineResult) -> list[str]:
    """
    Check a result against the rules Track B relies on. Returns a list of
    violations; empty list means the result is safe to hand over.

    Run this before emitting anything. It is cheap, and it catches the class of
    bug that would otherwise surface as a confidently-worded wrong narrative.
    """
    problems: list[str] = []

    if not result.provenance.snapshot_hash and result.provenance.snapshot_backend == "csv":
        problems.append("provenance.snapshot_hash missing -- result is not reproducible")

    for i, w in enumerate(result.waterfalls):
        loc = f"waterfalls[{i}] ({w.grain} {w.key})"
        if w.status is ResultStatus.COMPLETE and w.evidence.is_empty():
            problems.append(f"{loc}: COMPLETE but cites no evidence")
        if w.status is ResultStatus.COMPLETE and w.confidence.band is ConfidenceBand.NONE:
            problems.append(f"{loc}: COMPLETE with zero confidence")
        if w.confidence.band in (ConfidenceBand.LOW, ConfidenceBand.NONE) and not w.confidence.reasons:
            problems.append(f"{loc}: low confidence with no reason given")
        # arithmetic self-consistency, where all steps are present
        if all(m.amount is not None for m in (w.gross_value, w.on_invoice_discount, w.invoice_price)):
            if abs((w.gross_value.amount - w.on_invoice_discount.amount) - w.invoice_price.amount) > 0.01:
                problems.append(f"{loc}: invoice_price does not reconcile")
        if all(m.amount is not None for m in (w.invoice_price, w.off_invoice_leakage, w.pocket_price)):
            if abs((w.invoice_price.amount - w.off_invoice_leakage.amount) - w.pocket_price.amount) > 0.01:
                problems.append(f"{loc}: pocket_price does not reconcile")

    for i, r in enumerate(result.recommendations):
        if r.status is ResultStatus.COMPLETE and r.price_range is None:
            problems.append(f"recommendations[{i}]: COMPLETE but no price range")
        if r.comparable_deals_count == 0 and r.status is ResultStatus.COMPLETE:
            problems.append(f"recommendations[{i}]: no comparable deals but not abstained")

    for i, e in enumerate(result.elasticities):
        if e.beta is not None and e.credible_interval is None:
            problems.append(f"elasticities[{i}]: point estimate without a credible interval")

    return problems
