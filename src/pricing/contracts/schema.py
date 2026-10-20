"""The engine -> narrative result contract (D8): the Track A / Track B boundary."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..config import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM

CONTRACT_VERSION = "0.2.0-draft"


class ResultStatus(str, Enum):
    """Why a number is, or is not, present. Never guess from a null."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    ABSTAINED = "abstained"


class ConfidenceBand(str, Enum):
    """Coarse confidence label carried alongside the score."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True)
class Confidence:
    """Quantified confidence, plus the reasons Track B must be able to state."""

    score: float
    band: ConfidenceBand
    reasons: tuple[str, ...] = ()

    @staticmethod
    def from_score(score: float, reasons: tuple[str, ...] = ()) -> Confidence:
        """Clamp a score to 0-1 and assign its band."""
        score = max(0.0, min(1.0, float(score)))
        if score >= CONFIDENCE_HIGH:
            band = ConfidenceBand.HIGH
        elif score >= CONFIDENCE_MEDIUM:
            band = ConfidenceBand.MEDIUM
        elif score > 0.0:
            band = ConfidenceBand.LOW
        else:
            band = ConfidenceBand.NONE
        return Confidence(round(score, 4), band, tuple(reasons))


@dataclass(frozen=True)
class Money:
    """A monetary amount that can be absent for a stated reason."""

    amount: float | None
    currency: str = "EUR"
    status: ResultStatus = ResultStatus.COMPLETE
    note: str | None = None

    @staticmethod
    def unavailable(reason: str, currency: str = "EUR") -> Money:
        """An amount that could not be computed, and why."""
        return Money(None, currency, ResultStatus.UNAVAILABLE, reason)

    def __bool__(self) -> bool:
        """Truthiness means 'has an amount', so `if money:` cannot mean `if amount:`."""
        return self.amount is not None


@dataclass(frozen=True)
class Evidence:
    """Pointers to the graph records a number was computed from."""

    transaction_ids: tuple[str, ...] = ()
    customer_ids: tuple[str, ...] = ()
    product_ids: tuple[str, ...] = ()
    discount_ids: tuple[str, ...] = ()

    def count(self) -> int:
        """How many transactions back this result."""
        return len(self.transaction_ids)

    def is_empty(self) -> bool:
        """True when the record cites nothing at all."""
        return not any(
            (self.transaction_ids, self.customer_ids, self.product_ids, self.discount_ids)
        )


@dataclass(frozen=True)
class WaterfallRecord:
    """One price waterfall at whatever grain it was computed."""

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

    n_transactions: int = 0
    n_certified: int = 0

    @property
    def coverage_pct(self) -> float | None:
        """Share of this group's transactions that were fully decomposable."""
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
    """A low/mid/high price band, ordered by construction."""

    low: float
    mid: float
    high: float

    def __post_init__(self) -> None:
        """Reject an unordered band at construction time."""
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
    constraints_applied: tuple[str, ...] = ()


@dataclass(frozen=True)
class ElasticityEstimate:
    """Elasticity with mandatory uncertainty. (D5, Week 5.)"""

    scope: dict[str, Any]
    beta: float | None
    credible_interval: tuple[float, float] | None
    n_observations: int
    shrunk_toward_prior: bool
    fit_for_optimisation: bool
    status: ResultStatus
    confidence: Confidence
    evidence: Evidence


@dataclass(frozen=True)
class Provenance:
    """Where a result came from. Without this a result is not auditable."""

    snapshot_backend: str
    snapshot_location: str
    snapshot_hash: str | None
    contract_version: str = CONTRACT_VERSION
    engine_version: str = "0.4.0"
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    mlflow_run_id: str | None = None


@dataclass(frozen=True)
class EngineResult:
    """The single object Track B receives: one engine run, typed and provenanced."""

    provenance: Provenance
    waterfalls: tuple[WaterfallRecord, ...] = ()
    segments: tuple[SegmentAssignment, ...] = ()
    recommendations: tuple[PriceRecommendation, ...] = ()
    elasticities: tuple[ElasticityEstimate, ...] = ()
    data_quality_flags: tuple[dict[str, Any], ...] = ()
    period: tuple[Date | None, Date | None] = (None, None)

    def to_dict(self) -> dict:
        """The result as plain nested dicts."""
        return asdict(self, dict_factory=_dict_factory)

    def to_json(self, indent: int = 2) -> str:
        """The result as JSON, refusing to emit NaN or Infinity."""
        return json.dumps(self.to_dict(), indent=indent, default=_json_default, allow_nan=False)


def _dict_factory(items):
    """dataclasses.asdict hook, kept explicit for future field filtering."""
    return dict(items)


def _json_default(o):
    """Serialise the non-JSON types this contract uses."""
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (datetime, Date)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def validate(result: EngineResult) -> list[str]:
    """Check a result against the rules Track B relies on; empty list means safe."""
    problems: list[str] = []

    if not result.provenance.snapshot_hash and result.provenance.snapshot_backend == "csv":
        problems.append("provenance.snapshot_hash missing -- result is not reproducible")

    for i, w in enumerate(result.waterfalls):
        loc = f"waterfalls[{i}] ({w.grain} {w.key})"
        for name, money in (
            ("list_value", w.list_value), ("negotiated_off_list", w.negotiated_off_list),
            ("gross_value", w.gross_value), ("on_invoice_discount", w.on_invoice_discount),
            ("invoice_price", w.invoice_price), ("off_invoice_leakage", w.off_invoice_leakage),
            ("pocket_price", w.pocket_price), ("cogs", w.cogs), ("pocket_margin", w.pocket_margin),
        ):
            if money.amount is not None and not math.isfinite(money.amount):
                problems.append(f"{loc}: {name} is {money.amount}, not a finite number")
        for name, pct in (
            ("pocket_margin_pct", w.pocket_margin_pct),
            ("price_realisation_pct", w.price_realisation_pct),
        ):
            if pct is not None and not math.isfinite(pct):
                problems.append(f"{loc}: {name} is {pct}, not a finite number")
        if w.status is ResultStatus.COMPLETE and w.evidence.is_empty():
            problems.append(f"{loc}: COMPLETE but cites no evidence")
        if w.status is ResultStatus.COMPLETE and w.confidence.band is ConfidenceBand.NONE:
            problems.append(f"{loc}: COMPLETE with zero confidence")
        if w.confidence.band in (ConfidenceBand.LOW, ConfidenceBand.NONE) and not w.confidence.reasons:
            problems.append(f"{loc}: low confidence with no reason given")
        for name, start, step, end in (
            ("invoice_price", w.gross_value, w.on_invoice_discount, w.invoice_price),
            ("pocket_price", w.invoice_price, w.off_invoice_leakage, w.pocket_price),
        ):
            known = all(m.amount is not None for m in (start, step, end))
            if known and abs(start.amount - step.amount - end.amount) > 0.01:
                problems.append(f"{loc}: {name} does not reconcile")

    for i, r in enumerate(result.recommendations):
        if r.status is ResultStatus.COMPLETE and r.price_range is None:
            problems.append(f"recommendations[{i}]: COMPLETE but no price range")
        if r.comparable_deals_count == 0 and r.status is ResultStatus.COMPLETE:
            problems.append(f"recommendations[{i}]: no comparable deals but not abstained")

    for i, e in enumerate(result.elasticities):
        if e.beta is not None and e.credible_interval is None:
            problems.append(f"elasticities[{i}]: point estimate without a credible interval")

    return problems
