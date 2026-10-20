"""MLflow experiment tracking, on a local SQLite backend, optional by design."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{_REPO_ROOT / 'mlflow.db'}")
ARTIFACT_ROOT = os.getenv("MLFLOW_ARTIFACT_ROOT", str(_REPO_ROOT / "mlartifacts"))
EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT_NAME", "axiom-pricing-engine")


def _mlflow():
    """Import mlflow if it is installed, else None."""
    try:
        import mlflow  # noqa: PLC0415

        return mlflow
    except ImportError:
        return None


class _NullRun:
    """Stand-in when MLflow is unavailable: same interface, does nothing."""

    info = type("info", (), {"run_id": None})()

    def log_param(self, *a, **k):
        """Discard a parameter."""

    def log_metric(self, *a, **k):
        """Discard a metric."""

    def log_dict(self, *a, **k):
        """Discard a dict artefact."""

    def log_artifact(self, *a, **k):
        """Discard a file artefact."""

    def set_tag(self, *a, **k):
        """Discard a tag."""


@contextmanager
def run_context(run_name: str, tags: dict[str, str] | None = None) -> Iterator[Any]:
    """Open an MLflow run, degrading to a no-op rather than blocking a result."""
    mlflow = _mlflow()
    if mlflow is None:
        print("[tracking] mlflow not installed -- run not tracked (pip install mlflow)")
        yield _NullRun()
        return

    try:
        mlflow.set_tracking_uri(TRACKING_URI)
        if mlflow.get_experiment_by_name(EXPERIMENT) is None:
            mlflow.create_experiment(EXPERIMENT, artifact_location=Path(ARTIFACT_ROOT).as_uri())
        mlflow.set_experiment(EXPERIMENT)
    except Exception as exc:
        print(f"[tracking] backend unavailable -- run NOT tracked: {exc}")
        yield _NullRun()
        return

    with mlflow.start_run(run_name=run_name) as active:
        for k, v in (tags or {}).items():
            mlflow.set_tag(k, v)
        yield _Adapter(mlflow, active)


class _Adapter:
    """Thin wrapper so calling code never imports mlflow directly."""

    def __init__(self, mlflow, active):
        """Bind one active run."""
        self._mlflow = mlflow
        self.info = active.info

    def log_param(self, k, v):
        """Record a parameter."""
        self._mlflow.log_param(k, v)

    def log_metric(self, k, v):
        """Record a metric, skipping values that are absent."""
        if v is not None and not pd.isna(v):
            self._mlflow.log_metric(k, float(v))

    def log_dict(self, obj, filename):
        """Record a dict as a JSON artefact."""
        self._mlflow.log_dict(obj, filename)

    def log_artifact(self, path, artifact_path=None):
        """Record a file as an artefact."""
        self._mlflow.log_artifact(str(path), artifact_path)

    def set_tag(self, k, v):
        """Tag the run."""
        self._mlflow.set_tag(k, v)


def log_readiness(run, readiness) -> None:
    """Log the admission test: the score, its five checks, and what each deliverable can do."""
    if readiness is None:
        return
    run.log_metric("readiness_score", float(readiness.score))
    run.set_tag("readiness_band", readiness.band.value)
    for check in readiness.checks:
        run.log_metric(f"readiness__{check.name}", float(check.score))

    capabilities = readiness.capabilities
    for _, row in capabilities.iterrows():
        name, status = row["deliverable"], row["status"]
        run.set_tag(f"can__{name}", status)
        run.log_metric(f"can__{name}", {"READY": 2, "LIMITED": 1}.get(status, 0))
    run.log_dict(capabilities.to_dict(orient="records"), "readiness_capabilities.json")


def log_models(run, models: dict[str, Any] | None, profile_name: str | None = None) -> None:
    """Log what each model concluded, and what it declined to conclude."""
    if not models:
        return
    skipped = models.get("skipped") or {}
    run.log_metric("models_skipped", len(skipped))
    for name, reason in skipped.items():
        run.set_tag(f"skipped__{name}", (reason or "")[:480])

    elasticity = models.get("elasticity")
    if elasticity is not None:
        trusted = sum(1 for e in elasticity.estimates if e.fit_for_optimisation)
        run.log_metric("elasticity_scopes_fitted", int(elasticity.n_scopes_fitted))
        run.log_metric("elasticity_scopes_abstained", int(elasticity.n_scopes_abstained))
        run.log_metric("elasticity_scopes_trusted_for_pricing", trusted)
        run.log_metric("elasticity_prior_mean", float(elasticity.prior_mean))
        run.set_tag("elasticity_backend", elasticity.backend)
        run.set_tag("elasticity_scope", ",".join(elasticity.scope_columns))
        circularity = getattr(elasticity, "circularity", None)
        if circularity is not None:
            run.log_metric("elasticity_circularity_ratio", circularity.ratio)
            run.log_metric("elasticity_circularity_flagged", int(circularity.flagged))
            run.set_tag("elasticity_circularity", circularity.note[:480])
        for e in elasticity.estimates:
            label = e.scope.get("segment_label")
            if label is not None and e.beta is not None:
                key = "".join(ch if ch.isalnum() else "_" for ch in label)[:60]
                run.log_metric(f"elasticity_segment__{key}", e.beta)
                run.log_metric(f"elasticity_segment__{key}__low", e.low)
                run.log_metric(f"elasticity_segment__{key}__high", e.high)

    segmentation = models.get("segmentation")
    if segmentation is not None:
        run.log_metric("segments_found", int(segmentation.k))
        run.log_metric("segment_customers_abstained", int(segmentation.n_abstained))
        run.log_metric("segment_silhouette", segmentation.silhouette)
        run.log_metric("segment_kmeans_vs_hierarchical_ari", segmentation.method_agreement)
        run.set_tag("segment_method", segmentation.method)
        run.set_tag("segment_k_choice", segmentation.selection_note[:480])
        if len(segmentation.selection):
            run.log_dict(segmentation.selection.to_dict(orient="records"),
                         "segment_k_selection.json")

    links = models.get("links") or {}
    if links:
        run.log_dict(links, "model_links.json")
        for name, text in links.items():
            key = "".join(ch if ch.isalnum() else "_" for ch in name)
            run.set_tag(f"link__{key}", text[:480])

    acceptance = models.get("acceptance")
    if acceptance is not None:
        run.log_metric("acceptance_scopes", int(acceptance.n_scopes))
        run.log_metric("acceptance_scopes_abstained", int(acceptance.n_abstained))
        # Whether this is a real win rate or a concession benchmark changes what the number
        # means entirely, so it travels with the run rather than living in a docstring.
        run.set_tag("acceptance_mode", acceptance.mode)
        run.set_tag("acceptance_is_win_rate", str(bool(acceptance.is_win_rate)))
        if getattr(acceptance, "is_win_rate", False):
            # The win model's held-out report card, then the model itself as a new registry
            # version.
            for key, value in acceptance.metrics.items():
                run.log_metric(f"win_test__{key}", value)
            for key, value in acceptance.price_effect.items():
                run.log_metric(f"win_price_effect__{key}", value)
            for code, effect in getattr(acceptance, "segment_price_effects", {}).items():
                for key, value in effect.items():
                    if key != "label":
                        run.log_metric(f"win_segment__{code}__{key}"[:250], value)
            run.set_tag("win_model_uses_segment", str(getattr(acceptance, "uses_segments", False)))
            run.log_metric("win_model_passed_checks", int(acceptance.usable))
            run.log_dict(acceptance.calibration().to_dict(orient="records"),
                         "win_model_calibration.json")
            run.log_dict({"report": acceptance.report()}, "win_model_report.json")
            if isinstance(run, _Adapter) and profile_name and models.get("win_data") is not None:
                from .registry import register

                # Registration is bookkeeping: if it fails, the run's results
                # still stand, and the failure is said out loud rather than raised.
                try:
                    version = register(acceptance, profile_name, models["win_data"])
                except Exception as exc:
                    print(f"[tracking] win model NOT registered: {exc}")
                    run.set_tag("registration_error", str(exc)[:480])
                else:
                    if version:
                        run.set_tag("registered_win_model_version", version)

    recommendations = models.get("recommendations")
    if recommendations is not None and hasattr(recommendations, "__len__"):
        run.log_metric("price_recommendations", len(recommendations))


def log_engine_run(
    run,
    waterfall,
    aggregations: dict[str, pd.DataFrame] | None = None,
    result=None,
    extra_params: dict[str, Any] | None = None,
    readiness=None,
    models: dict[str, Any] | None = None,
) -> None:
    """Log one engine run: what produced it, what it says, and the result itself."""
    from .. import __version__
    from ..contracts.schema import CONTRACT_VERSION

    snap = waterfall.snapshot

    params = {
        "engine_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "snapshot_backend": snap.get("backend"),
        "snapshot_location": snap.get("location"),
        "snapshot_hash": (snap.get("content_sha256") or "n/a")[:16],
        "profile": waterfall.profile.name,
        "grain_policy": waterfall.grain_policy,
        "n_transactions": int(waterfall.totals.get("n_transactions", 0)),
        "grains": ",".join(sorted(aggregations)) if aggregations else "",
    }
    params.update(extra_params or {})
    for k, v in params.items():
        run.log_param(k, v)

    log_readiness(run, readiness)
    log_models(run, models, waterfall.profile.name)

    for key, value in waterfall.totals.items():
        run.log_metric(key, value)

    for _, row in waterfall.flags.iterrows():
        run.log_metric(f"flag_txn__{row['flag'].replace(' ', '_')}", row["n_transactions"])

    availability = waterfall.availability()
    for _, row in availability.iterrows():
        run.log_metric(f"available_pct__{row['step']}", row["availability_pct"])

    run.log_dict(
        {k: (None if pd.isna(v) else float(v)) for k, v in waterfall.totals.items()},
        "waterfall_totals.json",
    )
    run.log_dict(waterfall.flags.to_dict(orient="records"), "data_quality_flags.json")
    run.log_dict(availability.to_dict(orient="records"), "step_availability.json")

    tmp = Path(tempfile.mkdtemp(prefix="axiom_run_"))
    for name, df in (aggregations or {}).items():
        path = tmp / f"aggregate_{name}.csv"
        df.to_csv(path, index=False)
        run.log_artifact(path, "aggregations")

    if result is not None:
        path = tmp / "engine_result.json"
        path.write_text(result.to_json(), encoding="utf-8")
        run.log_artifact(path, "contract")

        from ..contracts.schema import validate as validate_contract

        problems = validate_contract(result)
        run.log_metric("contract_violations", len(problems))
        if problems:
            run.log_dict({"violations": problems}, "contract_violations.json")


