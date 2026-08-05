"""
MLflow experiment tracking.

Week 2 requirement: "stand up MLflow tracking". There are no trained models yet,
so what gets tracked is the *deterministic* engine run — and that is not a
placeholder. The Brief's reproducibility requirement is that any result can be
regenerated from versioned data plus a recorded run, and a waterfall total is
exactly the kind of number a client will later ask us to justify.

So each run logs:

    params   — snapshot hash, backend, allocation method, engine + contract
               version, grains computed, filters applied
    metrics  — every waterfall step, coverage, leakage ratios, flag counts
    artifacts— the totals, the aggregations, the data-quality flags, and the
               serialised result contract

The snapshot hash is the link between MLflow and DVC: DVC versions the bytes,
MLflow records which bytes a result came from.

BACKEND
-------
Local SQLite by default (`sqlite:///mlflow.db`) — a single file next to the
repo, no server, no external calls, matching the sovereign deployment target.

Not the old `file:./mlruns` store: MLflow 3.x put that in maintenance mode and
refuses to use it without an opt-out flag. SQLite is the recommended local
backend and is also what the model registry needs from Week 7 — the file store
never supported the registry, so we would have had to migrate anyway.

Point MLFLOW_TRACKING_URI at the docker-compose server (http://localhost:5000)
when you want the UI, or run:  mlflow ui --backend-store-uri sqlite:///mlflow.db

MLflow is an optional dependency here: if it is not installed, `run_context`
becomes a no-op so the engine still runs. Tracking should never be the reason a
result cannot be produced.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

# Resolved against the repo root, not the cwd, so `python scripts/...` and
# `pytest` from anywhere write to the same store.
_REPO_ROOT = Path(__file__).resolve().parents[3]
TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{_REPO_ROOT / 'mlflow.db'}")
ARTIFACT_ROOT = os.getenv("MLFLOW_ARTIFACT_ROOT", str(_REPO_ROOT / "mlartifacts"))
EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT_NAME", "axiom-pricing-engine")


def _mlflow():
    try:
        import mlflow  # noqa: PLC0415

        return mlflow
    except ImportError:
        return None


class _NullRun:
    """Stand-in when MLflow is unavailable. Same interface, does nothing."""

    info = type("info", (), {"run_id": None})()

    def log_param(self, *a, **k): ...
    def log_metric(self, *a, **k): ...
    def log_dict(self, *a, **k): ...
    def log_artifact(self, *a, **k): ...
    def set_tag(self, *a, **k): ...


@contextmanager
def run_context(run_name: str, tags: dict[str, str] | None = None) -> Iterator[Any]:
    """
    Open an MLflow run, or yield a no-op if MLflow is not installed.

        with run_context("waterfall") as run:
            log_engine_run(run, waterfall, ...)
    """
    mlflow = _mlflow()
    if mlflow is None:
        print("[tracking] mlflow not installed -- run not tracked (pip install mlflow)")
        yield _NullRun()
        return

    try:
        mlflow.set_tracking_uri(TRACKING_URI)
        # Create the experiment with an explicit artifact root the first time;
        # after that set_experiment just resolves it.
        if mlflow.get_experiment_by_name(EXPERIMENT) is None:
            mlflow.create_experiment(EXPERIMENT, artifact_location=Path(ARTIFACT_ROOT).as_uri())
        mlflow.set_experiment(EXPERIMENT)
    except Exception as exc:
        # Tracking must never be the reason a result cannot be produced. A
        # misconfigured or unavailable backend degrades to an untracked run and
        # says so loudly, rather than taking the pipeline down with it.
        # (Common cause: `mlflow-skinny` is installed, which omits the
        # SQLAlchemy store. Install full `mlflow`.)
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
        self._mlflow = mlflow
        self.info = active.info

    def log_param(self, k, v):
        self._mlflow.log_param(k, v)

    def log_metric(self, k, v):
        if v is not None and not pd.isna(v):
            self._mlflow.log_metric(k, float(v))

    def log_dict(self, obj, filename):
        self._mlflow.log_dict(obj, filename)

    def log_artifact(self, path, artifact_path=None):
        self._mlflow.log_artifact(str(path), artifact_path)

    def set_tag(self, k, v):
        self._mlflow.set_tag(k, v)


# --------------------------------------------------------------------------
def log_engine_run(
    run,
    waterfall,
    aggregations: dict[str, pd.DataFrame] | None = None,
    result=None,
    extra_params: dict[str, Any] | None = None,
) -> None:
    """Log everything about one engine run. Safe to call with a no-op run."""
    from .. import __version__
    from ..contracts.schema import CONTRACT_VERSION

    snap = waterfall.snapshot

    # --- params: what produced this result ---------------------------------
    params = {
        "engine_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "snapshot_backend": snap.get("backend"),
        "snapshot_location": snap.get("location"),
        "snapshot_hash": (snap.get("content_sha256") or "n/a")[:16],
        "allocation_method": waterfall.allocation_method,
        "n_transactions": int(waterfall.totals.get("n_transactions", 0)),
        "grains": ",".join(sorted(aggregations)) if aggregations else "",
    }
    params.update(extra_params or {})
    for k, v in params.items():
        run.log_param(k, v)

    # --- metrics: what the result says -------------------------------------
    for key, value in waterfall.totals.items():
        run.log_metric(key, value)

    for _, row in waterfall.flags.iterrows():
        run.log_metric(f"flag_txn__{row['flag'].replace(' ', '_')}", row["n_transactions"])

    # --- artifacts: the result itself --------------------------------------
    run.log_dict(
        {k: (None if pd.isna(v) else float(v)) for k, v in waterfall.totals.items()},
        "waterfall_totals.json",
    )
    run.log_dict(waterfall.flags.to_dict(orient="records"), "data_quality_flags.json")

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


def latest_runs(n: int = 10) -> pd.DataFrame:
    """Recent runs, for comparing results across snapshots."""
    mlflow = _mlflow()
    if mlflow is None:
        return pd.DataFrame()
    mlflow.set_tracking_uri(TRACKING_URI)
    exp = mlflow.get_experiment_by_name(EXPERIMENT)
    if exp is None:
        return pd.DataFrame()
    return mlflow.search_runs([exp.experiment_id], max_results=n)
