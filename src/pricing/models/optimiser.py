"""Constrained price optimisation (D6, Week 6): a recommended price, and its floor."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..contracts import schema as C
from ..engine.waterfall import Waterfall
from . import acceptance

#: Comparable deals below which a context is not quoted at all.
MIN_COMPARABLES = acceptance.MIN_COMPARABLES
#: Prices whose expected profit is within this fraction of the best are reported
#: as part of the recommended band.
TOLERANCE_OF_MAX = 0.02
#: The recommended price may not sit further than this from what comparable deals
#: actually paid. An optimum nobody has ever been charged is a hypothesis.
REFERENCE_BAND = 0.25
#: Minimum unit margin, as a share of price, that any recommendation must clear.
MARGIN_FLOOR = 0.05
GRID_POINTS = 121
#: Ceiling on confidence when acceptance is a concession benchmark rather than an observed win
#: rate.
PROXY_CONFIDENCE_CAP = 0.79


@dataclass(frozen=True)
class Recommendation:
    """One recommended price band for one customer-product context."""

    customer_id: str
    product_id: str
    low: float | None
    mid: float | None
    high: float | None
    reference_price: float | None
    unit_cost: float | None
    list_price: float | None
    acceptance: float | None
    expected_unit_margin: float | None
    current_price: float | None
    uplift_pct: float | None
    n_comparables: int
    status: str
    constraints: tuple[str, ...] = ()
    note: str = ""
    #: Set by the win-probability optimiser, which measures its own confidence;
    #: None leaves the benchmark's rule in charge.
    confidence_score: float | None = None
    confidence_reasons: tuple[str, ...] = ()
    #: The deals and quotes this recommendation was computed from.
    evidence_ids: tuple[str, ...] = ()
    currency: str = ""
    #: The volume rule (win-model path): the segment elasticity used, and the volume expected at
    #: the recommended mid price as a share of today's, with the range the elasticity's credible
    #: interval gives.
    elasticity_used: float | None = None
    volume_share_at_mid: float | None = None
    volume_share_low: float | None = None
    volume_share_high: float | None = None

    def to_contract(self, currency: str = "EUR", is_win_rate: bool = False) -> C.PriceRecommendation:
        """The recommendation as Track B receives it."""
        complete = self.status == "complete"
        currency = self.currency or currency
        if self.confidence_score is not None:
            score = self.confidence_score if complete else 0.0
            reasons = self.confidence_reasons if complete else (self.note,)
        else:
            reasons = _confidence_reasons(self)
            score = 0.0 if not complete else min(1.0, self.n_comparables / (3 * MIN_COMPARABLES))
        if not is_win_rate:
            score = min(score, PROXY_CONFIDENCE_CAP)
        return C.PriceRecommendation(
            customer_id=self.customer_id,
            product_id=self.product_id,
            price_range=(
                C.PriceRange(round(self.low, 2), round(self.mid, 2), round(self.high, 2))
                if complete
                else None
            ),
            expected_margin=(
                C.Money(round(self.expected_unit_margin, 2), currency)
                if complete and self.expected_unit_margin is not None
                else C.Money.unavailable(self.note or "no feasible price", currency)
            ),
            acceptance_probability=(
                round(self.acceptance, 4) if complete and self.acceptance is not None else None
            ),
            comparable_deals_count=self.n_comparables,
            status=C.ResultStatus.COMPLETE if complete else C.ResultStatus.ABSTAINED,
            confidence=C.Confidence.from_score(score, reasons),
            evidence=C.Evidence(
                transaction_ids=tuple(self.evidence_ids),
                customer_ids=(self.customer_id,),
                product_ids=(self.product_id,),
            ),
            constraints_applied=self.constraints,
        )


def _scope_for(
    acceptance: acceptance.AcceptanceModel, context: pd.DataFrame, product_id: str
) -> dict[str, str]:
    """The acceptance model's scope key for this context."""
    scope: dict[str, str] = {}
    for col in acceptance.scope_columns:
        if col in context.columns and len(context):
            scope[col] = str(context[col].iloc[0])
        else:
            scope[col] = str(product_id)
    return scope


def _confidence_reasons(rec: Recommendation) -> tuple[str, ...]:
    """Why a recommendation is less than certain. Never empty when it is not."""
    if rec.status != "complete":
        return (rec.note,)
    reasons = [acceptance.BENCHMARK_INTERPRETATION]
    if rec.n_comparables < 3 * MIN_COMPARABLES:
        reasons.append(
            f"{rec.n_comparables} comparable deals is a thin basis for an acceptance curve"
        )
    if rec.constraints:
        reasons.append(f"the optimum was bounded by: {', '.join(rec.constraints)}")
    return tuple(reasons)


def _feasible_grid(
    unit_cost: float,
    list_price: float,
    reference: float,
    margin_floor: float,
    reference_band: float,
) -> tuple[np.ndarray, list[str]]:
    """Prices that satisfy every constraint, and the names of the ones that bound."""
    constraints: list[str] = []

    floor_from_cost = unit_cost / (1.0 - margin_floor)
    banded_low = reference * (1.0 - reference_band)
    banded_high = reference * (1.0 + reference_band)

    if banded_low > floor_from_cost:
        low = banded_low
        constraints.append(
            f"at least {100 * (1 - reference_band):.0f}% of the comparable price"
        )
    else:
        low = floor_from_cost
        constraints.append(f"at least cost plus a {100 * margin_floor:.0f}% margin")

    if banded_high < list_price:
        high = banded_high
        constraints.append(
            f"at most {100 * (1 + reference_band):.0f}% of the comparable price"
        )
    else:
        high = list_price
        constraints.append("at most list price")

    if not np.isfinite(low) or not np.isfinite(high) or low > high:
        return np.array([]), constraints
    return np.linspace(low, high, GRID_POINTS), constraints


def recommend(
    w: Waterfall,
    acceptance: acceptance.AcceptanceModel,
    customer_id: str,
    product_id: str,
    margin_floor: float = MARGIN_FLOOR,
    reference_band: float = REFERENCE_BAND,
) -> Recommendation:
    """The expected-profit-maximising price band for one customer-product pair."""
    profile = w.profile
    facts = w.facts[w.facts["certified"]]
    context = facts[
        (facts["customer_id"] == customer_id) & (facts["product_id"] == product_id)
    ]

    scope = _scope_for(acceptance, context, product_id)
    curve_key = acceptance._key(scope) if acceptance.scope_columns else ()
    comparables = acceptance.curves.get(curve_key, np.array([]))

    blank = {
        "customer_id": customer_id, "product_id": product_id, "low": None, "mid": None, "high": None,
        "reference_price": None, "unit_cost": None, "list_price": None, "acceptance": None,
        "expected_unit_margin": None, "current_price": None, "uplift_pct": None,
        "n_comparables": len(comparables),
    }

    if len(comparables) < MIN_COMPARABLES:
        return Recommendation(
            **blank, status="abstained",
            note=f"{len(comparables)} comparable deals, fewer than the {MIN_COMPARABLES} required",
        )

    product = facts[facts["product_id"] == product_id]
    unit_cost = float(product["unit_cost"].dropna().median()) if "unit_cost" in product else np.nan
    list_price = float(product["list_price"].dropna().median()) if "list_price" in product else np.nan
    # The curve pools concession depths across the scope; the price band must not.
    reference = float(acceptance.product_reference.get(str(product_id), np.nan))
    if not np.isfinite(reference):
        reference = float(acceptance.reference_price.get(curve_key, np.nan))

    if not np.isfinite(unit_cost) or not np.isfinite(list_price) or not np.isfinite(reference):
        return Recommendation(
            **{**blank, "unit_cost": _finite(unit_cost), "list_price": _finite(list_price),
               "reference_price": _finite(reference)},
            status="abstained",
            note="unit cost, list price or a comparable price is missing for this product",
        )

    grid, constraints = _feasible_grid(
        unit_cost, list_price, reference, margin_floor, reference_band
    )
    if grid.size == 0:
        return Recommendation(
            **{**blank, "unit_cost": unit_cost, "list_price": list_price,
               "reference_price": reference},
            status="abstained",
            note=(
                "no price satisfies every constraint at once: the margin floor and the "
                "band around comparable prices do not overlap"
            ),
        )

    depths = 1.0 - grid / list_price
    probabilities = acceptance.curve(scope, depths)
    expected = probabilities * (grid - unit_cost)

    best = int(np.nanargmax(expected))
    peak = float(expected[best])
    within = expected >= peak * (1.0 - TOLERANCE_OF_MAX)

    # What this pair is paying today, which the uplift is measured against.
    recorded = profile.unit_price_column
    current_realised = (
        float(context[recorded].dropna().median())
        if recorded and recorded in context and len(context)
        else np.nan
    )
    current_list = (
        float((context[profile.opening] / context[profile.quantity_column]).median())
        if len(context) and profile.quantity_column in context
        else np.nan
    )
    baseline = current_realised if np.isfinite(current_realised) else current_list

    mid = float(grid[best])
    return Recommendation(
        customer_id=customer_id,
        product_id=product_id,
        low=float(grid[within].min()),
        mid=mid,
        high=float(grid[within].max()),
        reference_price=reference,
        unit_cost=unit_cost,
        list_price=list_price,
        acceptance=float(probabilities[best]),
        expected_unit_margin=peak,
        current_price=_finite(baseline),
        uplift_pct=(
            round(100 * (mid - baseline) / baseline, 2)
            if np.isfinite(baseline) and baseline
            else None
        ),
        n_comparables=len(comparables),
        status="complete",
        constraints=tuple(constraints),
    )


def recommend_book(
    w: Waterfall,
    acceptance: acceptance.AcceptanceModel | None = None,
    top: int | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Recommend for every customer-product pair the book has actually traded."""
    acceptance = acceptance or acceptance.fit(w)
    facts = w.facts[w.facts["certified"]]
    pairs = (
        facts.groupby(["customer_id", "product_id"])
        .agg(n_lines=("product_id", "size"))
        .reset_index()
        .sort_values("n_lines", ascending=False)
    )
    if top:
        pairs = pairs.head(top)

    rows = [
        vars(recommend(w, acceptance, row["customer_id"], row["product_id"], **kwargs))
        for _, row in pairs.iterrows()
    ]
    out = pd.DataFrame(rows)
    for col in ("constraints", "confidence_reasons", "evidence_ids"):
        out[col] = out[col].apply("; ".join)
    return out


def opportunity(recommendations: pd.DataFrame) -> pd.DataFrame:
    """The prize, split between the pairs that can be quoted and those that cannot."""
    complete = recommendations[recommendations["status"] == "complete"]
    liftable = complete[complete["uplift_pct"].fillna(0) > 0]
    return pd.DataFrame(
        [
            {
                "metric": "customer-product pairs considered",
                "value": len(recommendations),
            },
            {
                "metric": "pairs with a recommendation",
                "value": len(complete),
            },
            {
                "metric": "pairs not priced (reasons in the file)",
                "value": int((recommendations["status"] == "abstained").sum()),
            },
            {
                "metric": "pairs paying less today than the recommended price",
                "value": len(liftable),
            },
            {
                "metric": "median recommended increase for those pairs (%)",
                "value": round(float(liftable["uplift_pct"].median()), 2) if len(liftable) else 0.0,
            },
        ]
    )


def _finite(value) -> float | None:
    """A float, or None where the number does not exist."""
    if value is None or not np.isfinite(value):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# With a win-probability model (won and lost quotes)
# ---------------------------------------------------------------------------

#: The lowest chance of winning a recommendation may accept -- the volume and retention
#: constraint.
MIN_WIN_PROBABILITY = 0.25
#: Past quotes to a customer below which their situation is described too
#: thinly to trust the expected win rate fully.
THIN_CUSTOMER_DEALS = 5
#: Observed reference prices below which a product's money band is uncertain.
THIN_REFERENCE_OBSERVATIONS = 3
#: The volume rule: the lowest share of today's volume a recommended price may be expected to
#: keep, by the customer's segment elasticity.
MIN_VOLUME_SHARE = 0.80
#: Confidence multiplier when the volume rule could not run for a pair: one of
#: the guards the recommendation is meant to pass was not checked.
NO_VOLUME_RULE_FACTOR = 0.9


@dataclass(frozen=True)
class VolumeRule:
    """The elasticity behind one pair's volume rule, or why there is none."""

    beta: float | None = None
    low: float | None = None
    high: float | None = None
    scope: str = ""
    skipped: str = ""

    @property
    def applies(self) -> bool:
        """True when there is a usable elasticity to project volume with."""
        return self.beta is not None and not self.skipped

    def share(self, prices: np.ndarray, base: float, which: str = "low") -> np.ndarray:
        """Expected volume at each price, as a share of the volume at `base`."""
        beta = {"low": self.low, "mid": self.beta, "high": self.high}[which]
        return np.power(np.asarray(prices, dtype=float) / base, beta)


def volume_rule_for(elasticity, customer_id: str, product_id: str) -> VolumeRule:
    """Find the elasticity that governs a pair's volume, or say why there is none."""
    if elasticity is None:
        return VolumeRule(skipped="volume rule not applied: no elasticity was fitted")
    if not elasticity.any_fit_for_optimisation:
        return VolumeRule(
            skipped=(
                "volume rule not applied: no elasticity estimate passed the fit-for-"
                "optimisation gate, so volume at a new price cannot be projected"
            )
        )
    if elasticity.segment_of:
        estimate = elasticity.for_customer(customer_id)
        scope = f"segment {elasticity.segment_of.get(str(customer_id), '')}".strip()
        if estimate is None:
            return VolumeRule(
                skipped=(
                    "volume rule not applied: this customer has no segment (too few "
                    "deals to read a habit), so no segment elasticity applies"
                )
            )
    else:
        column = elasticity.scope_columns[0] if elasticity.scope_columns else "product_id"
        estimate = elasticity.for_scope(**{column: str(product_id)})
        scope = f"{column} {product_id}"
        if estimate is None:
            return VolumeRule(skipped=f"volume rule not applied: no elasticity for {scope}")
    if not estimate.fit_for_optimisation:
        return VolumeRule(
            skipped=(
                f"volume rule not applied: the elasticity for {scope} did not pass the "
                "fit-for-optimisation gate"
            )
        )
    return VolumeRule(beta=estimate.beta, low=estimate.low, high=estimate.high, scope=scope)


def _local_peaks(values: np.ndarray) -> int:
    """How many separate peaks a curve has."""
    steps = np.sign(np.diff(np.asarray(values, dtype=float)).round(9))
    steps = steps[steps != 0]
    if len(steps) == 0:
        return 1
    # A peak is a rise followed by a fall, or a curve that starts by falling
    # (peak at the first price) or ends by rising (peak at the last).
    inner = int(((steps[:-1] > 0) & (steps[1:] < 0)).sum())
    return inner + int(steps[0] < 0) + int(steps[-1] > 0)


def recommend_with_win_model(
    w: Waterfall,
    model,
    references,
    win_frame: pd.DataFrame,
    customer_id: str,
    product_id: str,
    margin_floor: float = MARGIN_FLOOR,
    min_win_probability: float = MIN_WIN_PROBABILITY,
    elasticity=None,
    min_volume_share: float = MIN_VOLUME_SHARE,
) -> tuple[Recommendation, pd.DataFrame]:
    """Expected-profit-maximising price band for one customer and product."""
    blank = {
        "customer_id": customer_id, "product_id": product_id, "low": None, "mid": None, "high": None,
        "reference_price": None, "unit_cost": None, "list_price": None, "acceptance": None,
        "expected_unit_margin": None, "current_price": None, "uplift_pct": None,
        "n_comparables": 0, "currency": references.currency,
    }
    empty_curve = pd.DataFrame()

    context = win_frame[win_frame["customer_id"] == customer_id]
    if context.empty:
        return Recommendation(
            **blank, status="abstained",
            note="no quotes to this customer on record, so their chance of winning is unknown",
        ), empty_curve
    if not model.usable:
        return Recommendation(
            **{**blank, "n_comparables": len(context)}, status="abstained",
            note="the win-probability model failed its checks: " + "; ".join(model.problems),
        ), empty_curve

    product = references.product(product_id)
    reference = float(product["reference_price"]) if product is not None else np.nan
    unit_cost = float(product["unit_cost"]) if product is not None else np.nan
    list_price = float(product["list_price"]) if product is not None else np.nan
    n_ref = int(product["n_reference_observations"]) if product is not None else 0
    if not np.isfinite(reference) or not np.isfinite(unit_cost) or not np.isfinite(list_price):
        return Recommendation(
            **{**blank, "n_comparables": len(context), "unit_cost": _finite(unit_cost),
               "list_price": _finite(list_price)},
            status="abstained",
            note=(
                "this product's reference price is unknown: it was never sold at the "
                "pricing model's suggested price (or a tested offset from it) on an order "
                "for this product alone"
            ),
        ), empty_curve

    low_pos, high_pos = model.position_range
    positions = np.linspace(low_pos, high_pos, GRID_POINTS)
    prices = reference * (1.0 + positions)
    win = model.curve(context, positions)
    unit_profit = prices - unit_cost
    expected = win * unit_profit

    # What this pair pays today, in the reference currency: the uplift is
    # measured against it, and the volume rule projects volume from it.
    facts = w.facts[w.facts["certified"]]
    pair = facts[(facts["customer_id"] == customer_id) & (facts["product_id"] == product_id)]
    paid = np.array(
        [
            references.to_reference_currency(p, c, d)
            for p, c, d in zip(
                pair[w.profile.unit_price_column], pair["currency_code"], pair["order_date"],
                strict=True,
            )
        ],
        dtype=float,
    )
    current = float(np.nanmedian(paid)) if np.isfinite(paid).any() else np.nan

    rule = volume_rule_for(elasticity, customer_id, product_id)
    base = current if np.isfinite(current) and current > 0 else reference
    if rule.applies:
        volume = rule.share(prices, base, "low")
        keeps_volume = volume >= min_volume_share
    else:
        volume = np.full(len(prices), np.nan)
        keeps_volume = np.ones(len(prices), dtype=bool)

    floor = unit_cost / (1.0 - margin_floor)
    feasible = (
        (prices >= floor) & (prices <= list_price) & (win >= min_win_probability) & keeps_volume
    )
    curve = pd.DataFrame(
        {
            "customer_id": customer_id,
            "product_id": product_id,
            "price": prices.round(2),
            "position_vs_reference_pct": (100 * positions).round(2),
            "win_probability": win.round(4),
            "profit_per_unit": unit_profit.round(2),
            "expected_profit_per_unit": expected.round(2),
            "volume_share_worst_case": np.round(volume, 4),
            "allowed": feasible,
        }
    )
    volume_text = (
        f"expected volume at least {100 * min_volume_share:.0f}% of today's "
        f"({rule.scope} elasticity, most elastic end {rule.low:.2f})"
        if rule.applies
        else ""
    )
    if not feasible.any():
        reasons = []
        if not (prices >= floor).any():
            reasons.append(f"every tested price is below cost plus {100 * margin_floor:.0f}%")
        if not (prices <= list_price).any():
            reasons.append("every tested price is above list price")
        if not (win >= min_win_probability).any():
            reasons.append(
                f"no tested price keeps the chance of winning at "
                f"{100 * min_win_probability:.0f}% or more"
            )
        if not keeps_volume.any():
            gap = (
                f"; today's price {base:,.2f} is {100 * (prices.min() / base - 1):.0f}% below "
                "the cheapest tested price"
                if prices.min() > base
                else ""
            )
            reasons.append(f"no tested price keeps {volume_text}{gap}")
        if not reasons:
            reasons.append(
                "the prices that clear the margin floor and the list price all break "
                f"the {100 * min_win_probability:.0f}% chance-of-winning rule"
                + (" or the volume rule" if rule.applies else "")
            )
        return Recommendation(
            **{**blank, "n_comparables": len(context), "unit_cost": unit_cost,
               "list_price": list_price, "reference_price": reference},
            status="abstained",
            note="no price meets every rule at once: " + "; ".join(reasons),
        ), curve

    allowed = np.where(feasible, expected, -np.inf)
    best = int(np.argmax(allowed))
    peak = float(expected[best])
    band = feasible & (expected >= peak - abs(peak) * TOLERANCE_OF_MAX)
    # The band is the run of allowed prices around the best one, not every
    # allowed price that happens to score as well somewhere else on the ladder.
    lo = best
    while lo > 0 and band[lo - 1]:
        lo -= 1
    hi = best
    while hi < len(band) - 1 and band[hi + 1]:
        hi += 1

    # Which rules shaped the answer: a rule binds when the best price on the
    # tested ladder, ignoring the rules, breaks it.
    free_best = int(np.argmax(expected))
    bound = []
    if prices[free_best] < floor:
        bound.append(f"at least cost plus a {100 * margin_floor:.0f}% margin")
    if prices[free_best] > list_price:
        bound.append("no higher than list price")
    if win[free_best] < min_win_probability:
        bound.append(f"chance of winning at least {100 * min_win_probability:.0f}%")
    if rule.applies and not keeps_volume[free_best]:
        bound.append(volume_text)
    if free_best in (0, len(prices) - 1):
        bound.append("within the range of prices tested in the past")

    # Confidence follows the evidence: how many of this customer's quotes describe their
    # situation, how often the product's reference price was observed, and whether the answer
    # had to be pushed against a rule.
    reasons = []
    customer_part = min(1.0, len(context) / THIN_CUSTOMER_DEALS)
    if len(context) < THIN_CUSTOMER_DEALS:
        reasons.append(f"only {len(context)} past quotes to this customer")
    reference_part = min(1.0, n_ref / THIN_REFERENCE_OBSERVATIONS)
    if n_ref < THIN_REFERENCE_OBSERVATIONS:
        reasons.append(f"this product's reference price was observed only {n_ref} time(s)")
    rule_part = 0.85 if bound else 1.0
    if bound:
        reasons.append("the best price was limited by: " + ", ".join(bound))
    peaks = _local_peaks(expected)
    if peaks > 1:
        reasons.append(f"expected profit has {peaks} peaks across the tested prices")
    segment_part = 1.0
    warnings = getattr(model, "segment_warnings", {}) or {}
    if warnings and "segment" in context.columns and len(context):
        code = str(context["segment"].iloc[0])
        if code in warnings:
            segment_part = 0.7
            reasons.append(warnings[code])
    score = (
        customer_part * reference_part * rule_part * (0.7 if peaks > 1 else 1.0) * segment_part
    )
    if not reasons:
        reasons.append(
            f"based on {len(context)} past quotes to this customer and {n_ref} observed "
            "reference prices for this product"
        )

    mid = float(prices[best])
    volume_fields: dict[str, float | None] = {}
    constraints = list(bound)
    if rule.applies:
        mid_share = float(rule.share(np.array([mid]), base, "mid")[0])
        worst = float(rule.share(np.array([mid]), base, "low")[0])
        best_case = float(rule.share(np.array([mid]), base, "high")[0])
        lo_share, hi_share = sorted((worst, best_case))
        volume_fields = {
            "elasticity_used": round(float(rule.beta), 4),
            "volume_share_at_mid": round(mid_share, 4),
            "volume_share_low": round(lo_share, 4),
            "volume_share_high": round(hi_share, 4),
        }
        reasons.append(
            f"at the mid price, volume is expected at {100 * mid_share:.0f}% of today's "
            f"(between {100 * lo_share:.0f}% and {100 * hi_share:.0f}%), by the "
            f"{rule.scope} elasticity {rule.beta:.2f} "
            f"[{rule.low:.2f}, {rule.high:.2f}]"
        )
    else:
        constraints.append(rule.skipped)
        reasons.append(rule.skipped)
        score *= NO_VOLUME_RULE_FACTOR

    deal_ids = tuple(context["deal_id"].astype(str))
    ref_ids = tuple(str(product.get("reference_ids") or "").split(";"))
    return Recommendation(
        customer_id=customer_id,
        product_id=product_id,
        low=float(prices[lo]),
        mid=mid,
        high=float(prices[hi]),
        reference_price=reference,
        unit_cost=unit_cost,
        list_price=list_price,
        acceptance=float(win[best]),
        expected_unit_margin=peak,
        current_price=_finite(current),
        uplift_pct=(
            round(100 * (mid - current) / current, 2) if np.isfinite(current) and current else None
        ),
        n_comparables=len(context),
        status="complete",
        constraints=tuple(constraints),
        confidence_score=round(float(score), 4),
        confidence_reasons=tuple(reasons),
        evidence_ids=tuple(dict.fromkeys(i for i in (*deal_ids, *ref_ids) if i)),
        currency=references.currency,
        **volume_fields,
    ), curve


def recommend_book_with_win_model(
    w: Waterfall,
    model,
    references,
    win_frame: pd.DataFrame,
    top: int | None = None,
    **kwargs,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recommendations and trade-off curves for the pairs the book has traded most."""
    facts = w.facts[w.facts["certified"]]
    pairs = (
        facts.groupby(["customer_id", "product_id"])
        .agg(n_lines=("product_id", "size"))
        .reset_index()
        .sort_values(["n_lines", "customer_id", "product_id"], ascending=[False, True, True])
    )
    if top:
        pairs = pairs.head(top)

    rows, curves = [], []
    for _, row in pairs.iterrows():
        rec, curve = recommend_with_win_model(
            w, model, references, win_frame, row["customer_id"], row["product_id"], **kwargs
        )
        rows.append(vars(rec))
        if len(curve):
            curves.append(curve)
    out = pd.DataFrame(rows)
    for col in ("constraints", "confidence_reasons", "evidence_ids"):
        out[col] = out[col].apply("; ".join)
    return out, (pd.concat(curves, ignore_index=True) if curves else pd.DataFrame())
