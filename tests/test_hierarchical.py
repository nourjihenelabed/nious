"""Hierarchical-backend tests: the estimator selection, and the model when PyMC is here."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.contracts import schema as C  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource  # noqa: E402
from pricing.models import elasticity, hierarchical  # noqa: E402

pymc_only = pytest.mark.skipif(not hierarchical.available(), reason="PyMC is not installed")


@pytest.fixture(scope="module")
def w() -> wf.Waterfall:
    """The waterfall over the synthetic snapshot."""
    return wf.compute(CsvGraphSource())


# -- choosing an estimator ---------------------------------------------------


def test_an_explicit_backend_is_never_second_guessed():
    """`backend="conjugate"` means conjugate, whatever else is installed."""
    assert elasticity.choose_backend("conjugate", 100) == ("conjugate", "")
    assert elasticity.choose_backend("pymc", 100) == ("pymc", "")


def test_auto_falls_back_above_the_sampling_threshold():
    """A dataset too big to sample gets the closed form, and is told why."""
    backend, note = elasticity.choose_backend("auto", hierarchical.MAX_ROWS_FOR_MCMC + 1)
    assert backend == "conjugate"
    assert "sampling threshold" in note


def test_auto_always_explains_itself():
    """A silent estimator switch is the kind of thing that surfaces in a review."""
    backend, note = elasticity.choose_backend("auto", 100)
    assert backend in ("pymc", "conjugate")
    assert (backend == "pymc") == (note == "")


def test_the_fitted_model_records_which_estimator_ran(w):
    """`backend` on the result, so a number can be attributed to a method."""
    model = elasticity.fit(w)
    assert model.backend in ("pymc", "conjugate")
    if model.backend == "conjugate":
        assert model.diagnostics is None, "no chains means no diagnostics, not empty ones"


def test_the_conjugate_backend_needs_nothing_installed(w):
    """The engine's default path must not depend on an optional extra."""
    model = elasticity.fit(w, backend="conjugate")
    assert model.backend == "conjugate"
    assert model.estimates
    assert all(e.r_hat is None for e in model.estimates)


def test_an_unusable_explicit_backend_raises_rather_than_falling_back(w, monkeypatch):
    """Asking for PyMC and silently getting something else would be worse than failing."""
    monkeypatch.setattr(hierarchical, "available", lambda: False)
    with pytest.raises(RuntimeError, match="PyMC is not installed"):
        hierarchical.fit(elasticity.observations(w), ["product_id"])


# -- the model itself --------------------------------------------------------


@pymc_only
@pytest.mark.slow
def test_the_hierarchical_fit_converges_and_reports_it(w):
    """Diagnostics are gates, so they have to actually be computed."""
    model = elasticity.fit(w, backend="pymc", draws=400, tune=400, chains=2)

    assert model.backend == "pymc"
    assert model.diagnostics is not None
    assert {"divergences", "max r_hat", "min ess_bulk"} <= set(model.diagnostics["diagnostic"])
    for e in model.estimates:
        assert e.r_hat is not None and e.ess_bulk is not None


@pymc_only
@pytest.mark.slow
def test_a_scope_that_did_not_converge_is_barred_from_optimisation(w):
    """A posterior from chains that did not mix is not a weaker answer."""
    model = elasticity.fit(w, backend="pymc", draws=400, tune=400, chains=2)
    for e in model.estimates:
        if e.r_hat is not None and e.r_hat > hierarchical.MAX_RHAT:
            assert not e.fit_for_optimisation
            assert any("r_hat" in r for r in e.reasons)


@pymc_only
@pytest.mark.slow
def test_the_between_scope_spread_is_inferred_not_assumed(w):
    """This is the whole difference from the conjugate backend."""
    model = elasticity.fit(w, backend="pymc", draws=400, tune=400, chains=2)
    assert model.prior_sd > 0, "sigma_beta must be estimated, not fixed at zero"
    assert all(e.interval is not None for e in model.estimates if e.beta is not None)


@pymc_only
@pytest.mark.slow
def test_heavy_pooling_is_reported_but_does_not_disqualify(w):
    """Under partial pooling, high shrinkage is a finding, not a defect."""
    model = elasticity.fit(w, backend="pymc", draws=400, tune=400, chains=2)
    pooled = [e for e in model.estimates if e.shrinkage > 0.5 and e.beta is not None]
    assert pooled, "expected heavy pooling on this snapshot"
    for e in pooled:
        assert any("pooled" in r for r in e.reasons)
        assert not any(
            "pooled" in r and "not this scope" in r for r in e.reasons
        ), "the conjugate wording must not leak into the hierarchical path"


@pymc_only
@pytest.mark.slow
def test_the_hierarchical_result_satisfies_the_contract(w):
    """Same contract, either estimator: no point estimate without an interval."""
    model = elasticity.fit(w, backend="pymc", draws=400, tune=400, chains=2)
    result = C.EngineResult(
        provenance=C.Provenance("csv", "/x", "hash"),
        elasticities=model.to_contract(),
    )
    assert not C.validate(result)


# -- guards ------------------------------------------------------------------


@pymc_only
def test_pooling_needs_more_than_one_scope(w):
    """With one group there is nothing to pool toward, and saying so beats a fit."""
    obs = elasticity.observations(w)
    one = obs[obs["product_id"] == obs["product_id"].iloc[0]]
    with pytest.raises(RuntimeError, match="at least two scopes"):
        hierarchical.fit(one, ["product_id"])


@pymc_only
def test_the_compiler_probe_is_cached_and_never_raises():
    """A failed probe is an answer. It must not surface as an exception."""
    first = hierarchical.c_backend_works()
    assert isinstance(first, bool)
    assert hierarchical.c_backend_works() is first
