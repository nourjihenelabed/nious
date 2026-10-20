"""The PyMC backend for elasticity: a real hierarchical model, fitted once for all scopes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

#: Above this many aggregated observations, sampling stops being interactive.
#: The conjugate backend takes over and says so rather than hanging.
MAX_ROWS_FOR_MCMC = 200_000

#: Sampler settings.
DEFAULT_DRAWS = 1_000
DEFAULT_TUNE = 1_000
DEFAULT_CHAINS = 4
TARGET_ACCEPT = 0.9

#: Chains are sampled sequentially by default.
DEFAULT_CORES = 1

#: Convergence thresholds.
MAX_RHAT = 1.01
MIN_ESS = 400


@dataclass(frozen=True)
class ScopePosterior:
    """One scope's marginal posterior, plus the diagnostics that qualify it."""

    key: tuple[str, ...]
    beta_mean: float
    beta_sd: float
    low: float
    high: float
    n_observations: int
    r_hat: float
    ess_bulk: float
    shrinkage: float
    converged: bool
    reasons: tuple[str, ...] = ()


@dataclass
class HierarchicalFit:
    """The whole fitted model: per-scope posteriors and the population parameters."""

    posteriors: dict[tuple[str, ...], ScopePosterior]
    mu_beta: float
    sigma_beta: float
    n_divergences: int
    max_rhat: float
    min_ess: float
    draws: int
    chains: int
    backend: str = "pymc"
    #: False when PyTensor ran on its NumPy backend because no C compiler was
    #: usable. The posterior is the same; it merely took longer to get.
    c_compiled: bool = True

    @property
    def converged(self) -> bool:
        """True when the sampler produced a posterior worth reading at all."""
        return (
            self.n_divergences == 0
            and self.max_rhat <= MAX_RHAT
            and self.min_ess >= MIN_ESS
        )

    def diagnostics(self) -> pd.DataFrame:
        """The sampler's own report on itself."""
        return pd.DataFrame(
            [
                {"diagnostic": "divergences", "value": self.n_divergences, "threshold": 0,
                 "ok": self.n_divergences == 0},
                {"diagnostic": "max r_hat", "value": round(self.max_rhat, 4),
                 "threshold": MAX_RHAT, "ok": self.max_rhat <= MAX_RHAT},
                {"diagnostic": "min ess_bulk", "value": round(self.min_ess, 1),
                 "threshold": MIN_ESS, "ok": self.min_ess >= MIN_ESS},
                {"diagnostic": "population mean slope", "value": round(self.mu_beta, 4),
                 "threshold": None, "ok": True},
                {"diagnostic": "between-scope sd", "value": round(self.sigma_beta, 4),
                 "threshold": None, "ok": True},
                {"diagnostic": "pytensor C backend", "value": self.c_compiled,
                 "threshold": None, "ok": True},
            ]
        )


def available() -> bool:
    """True when PyMC is installed and importable."""
    try:
        import pymc  # noqa: F401

        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def c_backend_works() -> bool:
    """Can the C++ toolchain actually compile, or does it only look installed?"""
    cxx = os.environ.get("CXX") or shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "probe.cpp"
            src.write_text("int main(){return 0;}" + chr(10), encoding="utf-8")
            done = subprocess.run(
                [cxx, str(src), "-o", str(Path(tmp) / "probe")],
                capture_output=True,
                check=False,
                timeout=60,
            )
            return done.returncode == 0
    except Exception:
        return False


def _prepare(obs: pd.DataFrame, scope_columns: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    """Index the observations by scope and centre log price within each scope."""
    frame = obs.copy()
    frame["_scope"] = list(zip(*(frame[c].astype(str) for c in scope_columns), strict=True))
    codes, uniques = pd.factorize(frame["_scope"], sort=True)
    frame["_code"] = codes
    frame["_log_price_c"] = frame["log_price"] - frame.groupby("_code")["log_price"].transform(
        "mean"
    )
    return frame, uniques


def fit(
    obs: pd.DataFrame,
    scope_columns: list[str],
    credible_mass: float = 0.94,
    draws: int = DEFAULT_DRAWS,
    tune: int = DEFAULT_TUNE,
    chains: int = DEFAULT_CHAINS,
    cores: int = DEFAULT_CORES,
    seed: int = 20260824,
    progressbar: bool = False,
) -> HierarchicalFit:
    """Fit the partial-pooling model over every scope at once."""
    if len(obs) > MAX_ROWS_FOR_MCMC:
        raise RuntimeError(
            f"{len(obs):,} aggregated observations exceeds MAX_ROWS_FOR_MCMC "
            f"({MAX_ROWS_FOR_MCMC:,}); the conjugate backend handles this size."
        )

    # A broken C toolchain is not a reason to refuse to sample -- PyTensor has a
    # pure-Python backend that gives identical results, just more slowly. But the
    # switch has to happen *before* PyTensor is imported, because it reads this at
    # import time, and PyMC imports PyTensor. Set it afterwards and every graph in
    # the model spawns a compiler that fails in turn: the same fit took 615s with
    # the flag set up front and over five hours with it set one line too late.
    #
    # Which is why this runs before `available()` -- that function imports PyMC,
    # and importing PyMC is exactly what closes this window.
    compiled = c_backend_works()
    if not compiled and "pytensor" not in sys.modules:
        flags = os.environ.get("PYTENSOR_FLAGS", "")
        os.environ["PYTENSOR_FLAGS"] = f"{flags},cxx=" if flags else "cxx="

    if not available():
        raise RuntimeError(
            "PyMC is not installed. `pip install 'axiom-pricing[models]'`, or use "
            "backend='conjugate'."
        )

    import arviz as az
    import pymc as pm

    frame, uniques = _prepare(obs, scope_columns)
    n_scopes = len(uniques)
    if n_scopes < 2:
        raise RuntimeError(
            "hierarchical pooling needs at least two scopes; with one there is "
            "nothing to pool toward"
        )

    code = frame["_code"].to_numpy()
    x = frame["_log_price_c"].to_numpy()
    y = frame["log_quantity"].to_numpy()

    with pm.Model(coords={"scope": [str(u) for u in uniques]}):
        # Priors on the population.
        mu_beta = pm.Normal("mu_beta", mu=-1.0, sigma=2.0)
        sigma_beta = pm.HalfNormal("sigma_beta", sigma=1.0)

        # Non-centred: sample the standardised offsets, not the slopes.
        z_beta = pm.Normal("z_beta", mu=0.0, sigma=1.0, dims="scope")
        beta = pm.Deterministic("beta", mu_beta + sigma_beta * z_beta, dims="scope")

        mu_alpha = pm.Normal("mu_alpha", mu=float(np.mean(y)), sigma=5.0)
        sigma_alpha = pm.HalfNormal("sigma_alpha", sigma=2.0)
        z_alpha = pm.Normal("z_alpha", mu=0.0, sigma=1.0, dims="scope")
        alpha = pm.Deterministic("alpha", mu_alpha + sigma_alpha * z_alpha, dims="scope")

        sigma = pm.HalfNormal("sigma", sigma=1.0)
        pm.Normal(
            "log_quantity",
            mu=alpha[code] + beta[code] * x,
            sigma=sigma,
            observed=y,
        )

        idata = pm.sample(
            draws=draws,
            tune=tune,
            chains=chains,
            cores=cores,
            target_accept=TARGET_ACCEPT,
            random_seed=seed,
            progressbar=progressbar,
            compute_convergence_checks=False,
        )

    summary = az.summary(
        idata,
        var_names=["beta", "mu_beta", "sigma_beta"],
        hdi_prob=credible_mass,
        round_to=None,
    )
    divergences = int(idata.sample_stats["diverging"].sum())

    beta_rows = summary.loc[summary.index.str.startswith("beta[")]
    # ArviZ names the interval columns after the mass asked for -- hdi_3%, hdi_97%
    # at 94% -- so read them positionally rather than hardcoding a mass.
    hdi_cols = sorted(c for c in summary.columns if c.startswith("hdi_"))
    low_col, high_col = hdi_cols[0], hdi_cols[-1]

    mu = float(summary.loc["mu_beta", "mean"])
    sigma_between = float(summary.loc["sigma_beta", "mean"])
    counts = frame.groupby("_code").size()

    posteriors: dict[tuple[str, ...], ScopePosterior] = {}
    for position, key in enumerate(uniques):
        row = beta_rows.iloc[position]
        r_hat = float(row["r_hat"])
        ess = float(row["ess_bulk"])

        # How far this scope's posterior sits from the population mean, relative to how far a
        # completely unpooled estimate would sit.
        own = _ols_slope(frame.loc[frame["_code"] == position])
        shrinkage = (
            0.0
            if own is None or abs(own - mu) < 1e-9
            else float(np.clip(1.0 - abs(float(row["mean"]) - mu) / abs(own - mu), 0.0, 1.0))
        )

        reasons = []
        if r_hat > MAX_RHAT:
            reasons.append(f"r_hat {r_hat:.3f} above {MAX_RHAT}: the chains did not agree")
        if ess < MIN_ESS:
            reasons.append(f"effective sample size {ess:.0f} below {MIN_ESS}")
        if divergences:
            reasons.append(
                f"{divergences} divergent transition(s) in the fit: the posterior "
                "geometry was not fully explored"
            )

        posteriors[tuple(key)] = ScopePosterior(
            key=tuple(key),
            beta_mean=float(row["mean"]),
            beta_sd=float(row["sd"]),
            low=float(row[low_col]),
            high=float(row[high_col]),
            n_observations=int(counts.get(position, 0)),
            r_hat=r_hat,
            ess_bulk=ess,
            shrinkage=shrinkage,
            converged=not reasons,
            reasons=tuple(reasons),
        )

    return HierarchicalFit(
        posteriors=posteriors,
        mu_beta=mu,
        sigma_beta=sigma_between,
        n_divergences=divergences,
        max_rhat=float(beta_rows["r_hat"].max()),
        min_ess=float(beta_rows["ess_bulk"].min()),
        draws=draws,
        chains=chains,
        c_compiled=compiled,
    )


def _ols_slope(group: pd.DataFrame) -> float | None:
    """The unpooled slope for one scope, used only to measure shrinkage."""
    x = group["_log_price_c"].to_numpy()
    y = group["log_quantity"].to_numpy()
    denominator = float((x**2).sum())
    if len(x) < 3 or denominator == 0:
        return None
    return float((x * (y - y.mean())).sum() / denominator)
