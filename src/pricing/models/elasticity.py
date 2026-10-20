"""Price elasticity (D5, Week 5): a slope that is never reported without its uncertainty."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from ..contracts import schema as C
from ..engine.waterfall import Waterfall

#: Observations below this and a scope cannot be fitted at all.
MIN_OBSERVATIONS = 8
#: Observations below this and a scope is fitted but not trusted for
#: optimisation. JUDGEMENT. A caution line, not a test.
TRUSTED_OBSERVATIONS = 20
#: Standard deviation of log price below which the slope is not identified: there was no
#: experiment, so there is nothing to learn from.
MIN_LOG_PRICE_SD = 0.02
#: Credible interval mass.
CREDIBLE_MASS = 0.94

DIVISION_BIAS_NOTE = (
    "price derived as value / quantity because the source records no unit price; "
    "quantity appears on both sides of the regression, which biases the slope "
    "downward on arithmetic alone"
)

IDENTIFICATION_NOTE = (
    "observational prices: no price was randomised, so a discount granted because "
    "a large order was coming will read as volume caused by the discount"
)


@dataclass(frozen=True)
class Estimate:
    """One scope's elasticity, with everything needed to decide whether to use it."""

    scope: dict[str, str]
    beta: float | None
    low: float | None
    high: float | None
    raw_beta: float | None
    standard_error: float | None
    n_observations: int
    log_price_sd: float
    r_squared: float | None
    shrinkage: float
    fit_for_optimisation: bool
    reasons: tuple[str, ...] = ()
    identification: str = IDENTIFICATION_NOTE
    #: Sampler diagnostics. None under the conjugate backend, which has no chains
    #: to diagnose -- absence here means "not applicable", not "not checked".
    r_hat: float | None = None
    ess_bulk: float | None = None
    #: The records the estimate was computed from, for the contract's evidence.
    #: Empty means "read it off the scope" (a product scope names its product).
    customer_ids: tuple[str, ...] = ()
    product_ids: tuple[str, ...] = ()

    @property
    def shrunk_toward_prior(self) -> bool:
        """True when the pooled prior moved this estimate materially."""
        return self.shrinkage > 0.25

    @property
    def interval(self) -> tuple[float, float] | None:
        """The credible interval, or None when there is no estimate."""
        if self.low is None or self.high is None:
            return None
        return (self.low, self.high)


@dataclass
class ElasticityModel:
    """Every scope's estimate, plus the pooled prior they were shrunk toward."""

    estimates: tuple[Estimate, ...]
    prior_mean: float
    prior_sd: float
    scope_columns: tuple[str, ...]
    n_scopes_fitted: int
    n_scopes_abstained: int
    price_source: str = "unknown"
    backend: str = "conjugate"
    backend_note: str = ""
    diagnostics: pd.DataFrame | None = None
    #: Set when the scope is a customer segment: the circularity check's verdict.
    circularity: Circularity | None = None
    #: Customer -> segment label used for the fit, so the optimiser can find the
    #: estimate that applies to a customer. Empty for dimension scopes.
    segment_of: dict[str, str] = field(default_factory=dict)

    def for_customer(self, customer_id: str) -> Estimate | None:
        """The estimate for a customer's segment, or None when they have none."""
        label = self.segment_of.get(str(customer_id))
        return None if label is None else self.for_scope(**{SEGMENT_SCOPE: label})

    @property
    def any_fit_for_optimisation(self) -> bool:
        """True when at least one estimate passed the gate."""
        return any(e.fit_for_optimisation for e in self.estimates)

    def to_frame(self) -> pd.DataFrame:
        """The estimates as a table, most confident first."""
        rows = []
        for e in self.estimates:
            row = dict(e.scope)
            row.update(
                {
                    "beta": e.beta,
                    "ci_low": e.low,
                    "ci_high": e.high,
                    "raw_beta": e.raw_beta,
                    "n_observations": e.n_observations,
                    "log_price_sd": round(e.log_price_sd, 4),
                    "r_squared": e.r_squared,
                    "shrinkage": round(e.shrinkage, 3),
                    "shrunk_toward_prior": e.shrunk_toward_prior,
                    "r_hat": e.r_hat,
                    "ess_bulk": e.ess_bulk,
                    "fit_for_optimisation": e.fit_for_optimisation,
                    "reasons": "; ".join(e.reasons),
                }
            )
            rows.append(row)
        out = pd.DataFrame(rows)
        if out.empty:
            return out
        return out.sort_values(
            ["fit_for_optimisation", "n_observations"], ascending=[False, False],
            ignore_index=True,
        )

    def for_scope(self, **scope) -> Estimate | None:
        """One scope's estimate by its key, or None when it was never fitted."""
        for e in self.estimates:
            if all(e.scope.get(k) == v for k, v in scope.items()):
                return e
        return None

    def to_contract(self) -> tuple[C.ElasticityEstimate, ...]:
        """The estimates as Track B receives them."""
        records = []
        for e in self.estimates:
            usable = e.beta is not None
            records.append(
                C.ElasticityEstimate(
                    scope=dict(e.scope),
                    beta=None if not usable else round(e.beta, 4),
                    credible_interval=(
                        None if not usable else (round(e.low, 4), round(e.high, 4))
                    ),
                    n_observations=e.n_observations,
                    shrunk_toward_prior=e.shrunk_toward_prior,
                    fit_for_optimisation=e.fit_for_optimisation,
                    status=(
                        C.ResultStatus.COMPLETE
                        if e.fit_for_optimisation
                        else (C.ResultStatus.ABSTAINED if usable else C.ResultStatus.UNAVAILABLE)
                    ),
                    confidence=C.Confidence.from_score(
                        _confidence_score(e), (*e.reasons, e.identification)
                    ),
                    evidence=C.Evidence(
                        customer_ids=tuple(e.customer_ids),
                        product_ids=tuple(e.product_ids) or (
                            (e.scope["product_id"],) if "product_id" in e.scope else ()
                        ),
                    ),
                )
            )
        return tuple(records)


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


def observations(
    w: Waterfall,
    scope: str | list[str] = "product",
    period: str = "period",
    facts: pd.DataFrame | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Price and quantity per scope per period, from certified line facts."""
    profile = w.profile
    quantity = profile.quantity_column
    level = _deepest_line_level(profile)
    if facts is None:
        facts = w.facts[w.facts["certified"]]

    cols = list(columns) if columns is not None else profile.resolve(scope)
    missing = [c for c in (*cols, period) if c not in facts.columns]
    if missing or not quantity or level is None:
        raise ValueError(
            f"cannot build elasticity observations for profile {profile.name!r}: "
            f"missing {missing or ['quantity or a line-grain price level']}"
        )

    recorded = profile.unit_price_column
    use_recorded = bool(recorded) and recorded in facts.columns

    agg = {
        "quantity": (quantity, "sum"),
        "value": (level, "sum"),
        "n_lines": (quantity, "size"),
    }
    if use_recorded:
        agg["price"] = (recorded, "median")

    grouped = facts.groupby([*cols, period], dropna=False).agg(**agg).reset_index()
    grouped = grouped[(grouped["quantity"] > 0) & (grouped["value"] > 0)].copy()

    if use_recorded:
        grouped["price_source"] = f"recorded {recorded}"
    else:
        grouped["price"] = grouped["value"] / grouped["quantity"]
        grouped["price_source"] = DIVISION_BIAS_NOTE

    grouped = grouped[grouped["price"] > 0].copy()
    grouped["log_price"] = np.log(grouped["price"])
    grouped["log_quantity"] = np.log(grouped["quantity"])
    return grouped


def _ols(x: np.ndarray, y: np.ndarray, absorbed: int = 0) -> tuple[float, float, float]:
    """Slope, its standard error, and R-squared for a simple regression."""
    n = len(x)
    x_centred = x - x.mean()
    denominator = float((x_centred**2).sum())
    if n < 3 or denominator == 0:
        return float("nan"), float("nan"), float("nan")

    slope = float((x_centred * (y - y.mean())).sum() / denominator)
    intercept = float(y.mean() - slope * x.mean())
    residual = y - (intercept + slope * x)
    dof = n - 2 - absorbed
    sigma2 = float((residual**2).sum()) / dof if dof > 0 else float("nan")
    se = float(np.sqrt(sigma2 / denominator)) if np.isfinite(sigma2) else float("nan")

    total = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((residual**2).sum()) / total if total > 0 else float("nan")
    return slope, se, r2


def _z_for(mass: float) -> float:
    """The two-sided normal quantile for a credible mass."""
    from scipy.stats import norm

    return float(norm.ppf(0.5 + mass / 2.0))


def choose_backend(name: str, n_rows: int) -> tuple[str, str]:
    """Resolve `backend="auto"` into a concrete choice, and say why."""
    from . import hierarchical

    if name != "auto":
        return name, ""
    if not hierarchical.available():
        return "conjugate", "PyMC is not installed; `pip install 'axiom-pricing[models]'`"
    if n_rows > hierarchical.MAX_ROWS_FOR_MCMC:
        return "conjugate", (
            f"{n_rows:,} aggregated observations is above the sampling threshold "
            f"({hierarchical.MAX_ROWS_FOR_MCMC:,})"
        )
    if not hierarchical.c_backend_works():
        return "conjugate", (
            "PyTensor cannot compile C on this machine, so sampling would run on "
            "its NumPy backend and take minutes rather than seconds. Install a "
            "working C++ toolchain, or pass backend='pymc' to sample anyway -- the "
            "posterior is identical either way."
        )
    return "pymc", ""


def fit(
    w: Waterfall,
    scope: str | list[str] = "product",
    period: str = "period",
    min_observations: int = MIN_OBSERVATIONS,
    credible_mass: float = CREDIBLE_MASS,
    backend: str = "auto",
    **sampler_kwargs,
) -> ElasticityModel:
    """Fit one elasticity per scope, pooled across scopes."""
    profile = w.profile
    obs = observations(w, scope, period)
    cols = profile.resolve(scope)
    return _fit_observations(
        obs, cols, min_observations, credible_mass, backend, **sampler_kwargs
    )


def _fit_observations(
    obs: pd.DataFrame,
    cols: list[str],
    min_observations: int,
    credible_mass: float,
    backend: str,
    absorbed: dict[tuple[str, ...], int] | None = None,
    prior_mean: float | None = None,
    pooling_is_gate: bool = True,
    **sampler_kwargs,
) -> ElasticityModel:
    """Fit one slope per scope in `obs` and shrink each toward the pooled prior."""
    absorbed = absorbed or {}
    resolved, note = choose_backend(backend, len(obs))
    if resolved == "pymc":
        try:
            return _fit_hierarchical(
                obs, cols, min_observations, credible_mass, **sampler_kwargs
            )
        except Exception as exc:
            if backend == "pymc":
                raise
            detail = str(exc).splitlines()[0][:180]
            note = f"the hierarchical fit failed ({type(exc).__name__}: {detail}); fell back"
            resolved = "conjugate"

    price_source = str(obs["price_source"].iloc[0]) if len(obs) else "unknown"
    derived_price = price_source == DIVISION_BIAS_NOTE

    raw: list[dict] = []
    for key, group in obs.groupby(cols, dropna=False):
        key_tuple = key if isinstance(key, tuple) else (key,)
        slope, se, r2 = _ols(
            group["log_price"].to_numpy(), group["log_quantity"].to_numpy(),
            absorbed=absorbed.get(tuple(str(k) for k in key_tuple), 0),
        )
        raw.append(
            {
                "scope": dict(zip(cols, (str(k) for k in key_tuple), strict=True)),
                "n": len(group),
                "slope": slope,
                "se": se,
                "r2": r2,
                "log_price_sd": float(group["log_price"].std(ddof=1)) if len(group) > 1 else 0.0,
            }
        )

    # The pooled prior comes only from scopes whose own slope is worth pooling.
    usable = [
        r for r in raw
        if r["n"] >= min_observations
        and np.isfinite(r["slope"])
        and np.isfinite(r["se"])
        and r["log_price_sd"] >= MIN_LOG_PRICE_SD
    ]
    slopes = np.array([r["slope"] for r in usable]) if usable else np.array([])
    if prior_mean is None:
        prior_mean = float(slopes.mean()) if len(slopes) else -1.0
    prior_sd = float(slopes.std(ddof=1)) if len(slopes) > 1 else 1.0
    prior_sd = prior_sd if prior_sd > 1e-6 else 1.0

    z = _z_for(credible_mass)
    tau2 = prior_sd**2
    estimates: list[Estimate] = []
    n_fitted = 0

    for r in raw:
        reasons: list[str] = []
        if r["n"] < min_observations:
            reasons.append(f"{r['n']} observations, fewer than the {min_observations} required")
        if r["log_price_sd"] < MIN_LOG_PRICE_SD:
            reasons.append(
                f"realised price barely moved (sd of log price {r['log_price_sd']:.4f}); "
                "there is no price variation to learn a slope from"
            )
        if not np.isfinite(r["slope"]) or not np.isfinite(r["se"]):
            reasons.append("the regression is degenerate at this scope")

        if reasons:
            estimates.append(
                Estimate(
                    scope=r["scope"], beta=None, low=None, high=None,
                    raw_beta=_finite(r["slope"]), standard_error=_finite(r["se"]),
                    n_observations=r["n"], log_price_sd=r["log_price_sd"],
                    r_squared=_finite(r["r2"]), shrinkage=1.0,
                    fit_for_optimisation=False, reasons=tuple(reasons),
                )
            )
            continue

        se2 = max(r["se"] ** 2, 1e-12)
        weight = tau2 / (tau2 + se2)          # 1 = keep own slope, 0 = take the prior
        posterior = weight * r["slope"] + (1 - weight) * prior_mean
        posterior_sd = float(np.sqrt(1.0 / (1.0 / tau2 + 1.0 / se2)))
        low, high = posterior - z * posterior_sd, posterior + z * posterior_sd

        gate: list[str] = []
        if r["n"] < TRUSTED_OBSERVATIONS:
            gate.append(
                f"{r['n']} observations is below the {TRUSTED_OBSERVATIONS} this module "
                "trusts for optimisation"
            )
        if high >= 0:
            gate.append(
                f"the credible interval [{low:.2f}, {high:.2f}] includes zero or a "
                "positive slope, so demand may not respond to price here at all"
            )
        info: list[str] = []
        if 1 - weight > 0.5:
            if pooling_is_gate:
                gate.append(
                    f"{100 * (1 - weight):.0f}% of this estimate is the pooled prior, not "
                    "this scope's own evidence"
                )
            else:
                # The prior is the book-wide slope fitted on every scope's points, this one's
                # included.
                info.append(
                    f"{100 * (1 - weight):.0f}% pooled toward the book average "
                    f"({prior_mean:.2f}): the scopes differ by about {prior_sd:.2f} "
                    "from each other, less than each one's own uncertainty"
                )
        if derived_price:
            gate.append(DIVISION_BIAS_NOTE)

        fit_ok = not gate
        n_fitted += 1
        estimates.append(
            Estimate(
                scope=r["scope"], beta=float(posterior), low=float(low), high=float(high),
                raw_beta=float(r["slope"]), standard_error=float(r["se"]),
                n_observations=r["n"], log_price_sd=r["log_price_sd"],
                r_squared=_finite(r["r2"]), shrinkage=float(1 - weight),
                fit_for_optimisation=fit_ok, reasons=tuple(gate + info),
            )
        )

    return ElasticityModel(
        estimates=tuple(estimates),
        prior_mean=prior_mean,
        prior_sd=prior_sd,
        scope_columns=tuple(cols),
        n_scopes_fitted=n_fitted,
        n_scopes_abstained=len(estimates) - n_fitted,
        price_source=price_source,
        backend="conjugate",
        backend_note=note,
    )


def _fit_hierarchical(
    obs: pd.DataFrame,
    cols: list[str],
    min_observations: int,
    credible_mass: float,
    **sampler_kwargs,
) -> ElasticityModel:
    """Fit every scope in one partial-pooling model and translate the posteriors."""
    from . import hierarchical

    price_source = str(obs["price_source"].iloc[0]) if len(obs) else "unknown"
    derived_price = price_source == DIVISION_BIAS_NOTE

    result = hierarchical.fit(obs, cols, credible_mass=credible_mass, **sampler_kwargs)
    price_sd = obs.groupby(cols)["log_price"].std(ddof=1)

    estimates: list[Estimate] = []
    n_fitted = 0
    for key, posterior in result.posteriors.items():
        scope = dict(zip(cols, key, strict=True))
        lookup = key[0] if len(key) == 1 else key
        log_price_sd = float(price_sd.get(lookup, 0.0) or 0.0)

        gate: list[str] = list(posterior.reasons)
        info: list[str] = []
        if posterior.n_observations < TRUSTED_OBSERVATIONS:
            gate.append(
                f"{posterior.n_observations} observations is below the "
                f"{TRUSTED_OBSERVATIONS} this module trusts for optimisation"
            )
        if log_price_sd < MIN_LOG_PRICE_SD:
            gate.append(
                f"realised price barely moved (sd of log price {log_price_sd:.4f}); "
                "there is no price variation to learn a slope from"
            )
        if posterior.high >= 0:
            gate.append(
                f"the credible interval [{posterior.low:.2f}, {posterior.high:.2f}] "
                "includes zero or a positive slope, so demand may not respond to "
                "price here at all"
            )
        if posterior.shrinkage > 0.5:
            # Heavy pooling is a *gate* under the conjugate backend, where it means "your own
            # data was too thin to trust, so we borrowed".
            info.append(
                f"{100 * posterior.shrinkage:.0f}% pooled: the model inferred a "
                f"between-scope spread of {result.sigma_beta:.2f}, so this scope's "
                "slope is close to the population slope by estimation rather than "
                "by assumption"
            )
        if derived_price:
            gate.append(DIVISION_BIAS_NOTE)

        usable = posterior.n_observations >= min_observations
        n_fitted += bool(usable)
        estimates.append(
            Estimate(
                scope=scope,
                beta=posterior.beta_mean if usable else None,
                low=posterior.low if usable else None,
                high=posterior.high if usable else None,
                raw_beta=None,
                standard_error=posterior.beta_sd,
                n_observations=posterior.n_observations,
                log_price_sd=log_price_sd,
                r_squared=None,
                shrinkage=posterior.shrinkage,
                fit_for_optimisation=usable and not gate,
                reasons=tuple(gate + info) if usable else (
                    f"{posterior.n_observations} observations, fewer than the "
                    f"{min_observations} required",
                ),
                r_hat=posterior.r_hat,
                ess_bulk=posterior.ess_bulk,
            )
        )

    return ElasticityModel(
        estimates=tuple(estimates),
        prior_mean=result.mu_beta,
        prior_sd=result.sigma_beta,
        scope_columns=tuple(cols),
        n_scopes_fitted=n_fitted,
        n_scopes_abstained=len(estimates) - n_fitted,
        price_source=price_source,
        backend="pymc",
        backend_note=(
            "" if result.converged else "the sampler did not fully converge; see diagnostics"
        ),
        diagnostics=result.diagnostics(),
    )


def _finite(value) -> float | None:
    """A float, or None where the number does not exist."""
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def _confidence_score(e: Estimate) -> float:
    """Confidence in one estimate, 0 to 1."""
    if e.beta is None or e.low is None or e.high is None:
        return 0.0
    depth = min(1.0, e.n_observations / TRUSTED_OBSERVATIONS)
    if SEGMENT_SCOPE in e.scope:
        precision = 1.0 - (e.high - e.low) / (2.0 * abs(e.beta)) if e.beta else 0.0
        return max(0.0, min(1.0, precision)) * depth
    return max(0.0, 1.0 - e.shrinkage) * depth


def _deepest_line_level(profile) -> str | None:
    """The lowest level in the chain still knowable per line."""
    from ..analytics.margin import _deepest_line_level as level

    return level(profile)


# ---------------------------------------------------------------------------
# Segmentation -> elasticity
# ---------------------------------------------------------------------------

#: The scope key an estimate carries when it was measured inside a segment.
SEGMENT_SCOPE = "segment_label"
#: Segmentation features that describe how a customer pays rather than how they buy.
PRICE_BEHAVIOUR_FEATURES: tuple[str, ...] = (
    "price_realisation_pct",
    "total_leakage_pct",
    "off_invoice_intensity",
    "pocket_margin_pct",
    "realisation_dispersion",
)
#: Segments formed from price behaviour may differ in price sensitivity by up to this many times
#: as much as segments formed without it, before the difference is suspected of being made by the
#: segmentation.
CIRCULARITY_RATIO = 2.0
#: Spread (standard deviation of segment slopes) treated as "no spread at all",
#: so a near-zero price-blind spread does not turn noise into a large ratio.
MIN_SPREAD = 0.10

SEGMENT_NOTE = (
    "measured inside one customer segment with each product's own price and volume "
    "level removed, so the slope compares the same product at different prices, not "
    "cheap products with dear ones"
)


@dataclass(frozen=True)
class Circularity:
    """Whether segment elasticities could be an echo of how the segments were made."""

    price_features_used: tuple[str, ...]
    spread_with_price_features: float | None
    spread_price_blind: float | None
    ratio: float | None
    flagged: bool
    note: str


def segment_observations(
    w: Waterfall, segment_of: dict[str, str], period: str = "period"
) -> pd.DataFrame:
    """Price/volume points per segment, product and period, product levels removed."""
    product = w.profile.resolve("product")[0]
    facts = w.facts[w.facts["certified"]]
    facts = facts.assign(
        **{SEGMENT_SCOPE: facts["customer_id"].astype(str).map(segment_of)}
    )
    facts = facts[facts[SEGMENT_SCOPE].notna()]
    obs = observations(w, period=period, facts=facts, columns=[SEGMENT_SCOPE, product])
    keys = [SEGMENT_SCOPE, product]
    obs = obs[obs.groupby(keys)["log_price"].transform("size") >= 2].copy()
    for col in ("log_price", "log_quantity"):
        obs[f"{col}_level"] = obs[col]
        obs[col] = obs[col] - obs.groupby(keys)[col].transform("mean")
    return obs


def fit_by_segment(
    w: Waterfall,
    segmentation,
    period: str = "period",
    min_observations: int = MIN_OBSERVATIONS,
    credible_mass: float = CREDIBLE_MASS,
    backend: str = "auto",
    check_circularity: bool = True,
    **sampler_kwargs,
) -> ElasticityModel:
    """One elasticity per customer segment, thin segments pulled to the book average."""
    segment_of = {str(k): v for k, v in segmentation.label_of().items()}
    obs = segment_observations(w, segment_of, period)
    product = w.profile.resolve("product")[0]
    if obs.empty:
        raise ValueError(
            "no segment bought the same product in two periods, so there is no "
            "within-product price variation to measure"
        )

    per_segment = obs.groupby(SEGMENT_SCOPE)[product].nunique()
    absorbed = {(str(seg),): int(n) - 1 for seg, n in per_segment.items()}
    n_series = int(obs[[SEGMENT_SCOPE, product]].drop_duplicates().shape[0])
    pooled, _, _ = _ols(
        obs["log_price"].to_numpy(), obs["log_quantity"].to_numpy(), absorbed=n_series - 1
    )

    model = _fit_observations(
        obs, [SEGMENT_SCOPE], min_observations, credible_mass, backend,
        absorbed=absorbed, prior_mean=pooled if np.isfinite(pooled) else None,
        pooling_is_gate=False, **sampler_kwargs,
    )

    customers: dict[str, list[str]] = {}
    for customer, label in segment_of.items():
        customers.setdefault(label, []).append(customer)
    products = obs.groupby(SEGMENT_SCOPE)[product].unique()
    identification = f"{IDENTIFICATION_NOTE}; {SEGMENT_NOTE}"

    circularity = (
        circularity_check(w, segmentation, period, min_observations, credible_mass)
        if check_circularity
        else None
    )

    estimates = []
    for e in model.estimates:
        label = e.scope[SEGMENT_SCOPE]
        reasons = list(e.reasons)
        fit_ok = e.fit_for_optimisation
        if circularity is not None:
            if circularity.flagged:
                fit_ok = False
            reasons.append(circularity.note)
        estimates.append(
            replace(
                e,
                fit_for_optimisation=fit_ok,
                reasons=tuple(reasons),
                identification=identification,
                customer_ids=tuple(sorted(customers.get(label, ()))),
                product_ids=tuple(sorted(str(p) for p in products.get(label, ()))),
            )
        )
    model.estimates = tuple(estimates)
    model.circularity = circularity
    model.segment_of = segment_of
    return model


def _spread(model: ElasticityModel, min_observations: int) -> float | None:
    """How much the segments' own slopes differ: standard deviation of raw slopes."""
    slopes = [
        e.raw_beta for e in model.estimates
        if e.raw_beta is not None and e.n_observations >= min_observations
    ]
    return float(np.std(slopes)) if len(slopes) >= 2 else None


def circularity_check(
    w: Waterfall,
    segmentation,
    period: str = "period",
    min_observations: int = MIN_OBSERVATIONS,
    credible_mass: float = CREDIBLE_MASS,
) -> Circularity:
    """Compare segment elasticities with and without price features in the clustering."""
    from . import segmentation as segmentation_model

    used = tuple(segmentation.features_used or segmentation_model.FEATURES)
    price_features = tuple(f for f in used if f in PRICE_BEHAVIOUR_FEATURES)
    blind_features = tuple(f for f in used if f not in PRICE_BEHAVIOUR_FEATURES)
    if not price_features:
        return Circularity(
            (), None, None, None, False,
            "circularity check: the segments were formed without any price feature, "
            "so their price sensitivity cannot be an echo of how they were made",
        )
    if not blind_features:
        return Circularity(
            price_features, None, None, None, True,
            "circularity check failed: the segments were formed from price behaviour "
            "only, so there is no price-blind grouping to compare them with",
        )

    aware = fit_by_segment(
        w, segmentation, period, min_observations, credible_mass,
        backend="conjugate", check_circularity=False,
    )
    try:
        blind_segments = segmentation_model.fit(
            w, k=segmentation.k, features_used=blind_features
        )
        blind = fit_by_segment(
            w, blind_segments, period, min_observations, credible_mass,
            backend="conjugate", check_circularity=False,
        )
    except ValueError as exc:
        return Circularity(
            price_features, _spread(aware, min_observations), None, None, True,
            f"circularity check could not run ({exc}); segment elasticities are not "
            "used for optimisation until it can",
        )

    with_price = _spread(aware, min_observations)
    without = _spread(blind, min_observations)
    if with_price is None or without is None:
        return Circularity(
            price_features, with_price, without, None, False,
            "circularity check: fewer than two segments had enough points to compare, "
            "so the check could not measure a difference",
        )
    ratio = with_price / max(without, MIN_SPREAD)
    flagged = ratio > CIRCULARITY_RATIO
    named = ", ".join(price_features)
    if flagged:
        note = (
            f"circularity check failed: segments built partly from price behaviour "
            f"({named}) differ in price sensitivity {ratio:.1f} times as much as "
            f"segments built without it (spread {with_price:.2f} against "
            f"{without:.2f}), so part of the difference may come from how the "
            "segments were made; not used for optimisation"
        )
    else:
        note = (
            f"circularity check passed: segments built without price behaviour "
            f"differ about as much (spread {without:.2f} against {with_price:.2f}, "
            f"ratio {ratio:.1f}, limit {CIRCULARITY_RATIO:.1f}), so the segment "
            "differences are not an echo of the price features used to form them"
        )
    return Circularity(price_features, with_price, without, round(ratio, 3), flagged, note)
