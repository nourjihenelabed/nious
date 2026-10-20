"""Model registry (D7): every win-probability model versioned, one of them in charge."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .mlflow_runs import TRACKING_URI, _mlflow

CHAMPION = "champion"
CANDIDATE = "candidate"
PREVIOUS = "previous_champion"
#: Types the saved pipeline may contain beyond scikit-learn's own.
TRUSTED_TYPES = ["numpy.dtype"]
#: How much worse (in log loss) a candidate may score than the champion and still be promoted.
LOG_LOSS_TOLERANCE = 0.005


def model_name(profile_name: str) -> str:
    """The registered-model name for a profile's win-probability model."""
    return f"axiom-win-probability-{profile_name}"


def _client():
    """An MLflow client on the project's tracking store, or None without MLflow."""
    mlflow = _mlflow()
    if mlflow is None:
        return None, None
    mlflow.set_tracking_uri(TRACKING_URI)
    return mlflow, mlflow.MlflowClient()


def register(model, profile_name: str, data) -> str | None:
    """Log the fitted model to the active run and register it as the new candidate."""
    mlflow, client = _client()
    if mlflow is None or mlflow.active_run() is None:
        return None
    import mlflow.sklearn  # noqa: PLC0415

    name = model_name(profile_name)
    example = data.priced[[*model.numeric, *model.categorical]].head(5).copy()
    for col in model.numeric:
        example[col] = example[col].astype(float)
    for col in model.categorical:
        example[col] = example[col].astype(str)
    info = mlflow.sklearn.log_model(
        model.pipeline, name="win_model", registered_model_name=name, input_example=example,
        skops_trusted_types=TRUSTED_TYPES,
    )
    version = str(info.registered_model_version)

    tags = {
        "profile": profile_name,
        "trained_through": model.trained_through,
        "holdout_cutoff": model.holdout_cutoff,
        "usable": str(model.usable),
        "problems": "; ".join(model.problems)[:4000],
        "numeric_features": ",".join(model.numeric),
        "categorical_features": ",".join(model.categorical),
        "position_low": str(model.position_range[0]),
        "position_high": str(model.position_range[1]),
        "n_train": str(model.n_train),
        "n_test": str(model.n_test),
    }
    tags.update({f"test_{k}": f"{v:.6f}" for k, v in model.metrics.items()})
    for key, value in tags.items():
        client.set_model_version_tag(name, version, key, value)
    client.set_registered_model_alias(name, CANDIDATE, version)
    return version


@dataclass
class Decision:
    """The outcome of a promotion attempt, in words."""

    promoted: bool
    version: str | None
    message: str


def _version_tags(client, name: str, alias: str) -> tuple[str, dict[str, str]] | None:
    """A version behind an alias, and its tags; None if the alias is unset."""
    try:
        mv = client.get_model_version_by_alias(name, alias)
    except Exception:
        return None
    return str(mv.version), dict(mv.tags)


def promote(profile_name: str, version: str | None = None, force: bool = False) -> Decision:
    """Make a candidate the champion, if it has earned it."""
    mlflow, client = _client()
    if mlflow is None:
        return Decision(False, None, "MLflow is not installed.")
    name = model_name(profile_name)

    if version is None:
        found = _version_tags(client, name, CANDIDATE)
        if found is None:
            return Decision(False, None, f"No candidate model registered under {name}.")
        version, tags = found
    else:
        tags = dict(client.get_model_version(name, version).tags)

    champion = _version_tags(client, name, CHAMPION)
    if champion and champion[0] == version:
        return Decision(False, version, f"Version {version} is already the champion.")

    if not force:
        if tags.get("usable") != "True":
            return Decision(
                False, version,
                f"Version {version} failed its own checks and was not promoted: "
                f"{tags.get('problems') or 'see its run'}.",
            )
        if champion is not None:
            new_loss = float(tags.get("test_log_loss", "nan"))
            old_loss = float(champion[1].get("test_log_loss", "nan"))
            if not new_loss <= old_loss + LOG_LOSS_TOLERANCE:
                return Decision(
                    False, version,
                    f"Version {version} predicts worse on unseen deals than the champion "
                    f"(version {champion[0]}): log loss {new_loss:.4f} against {old_loss:.4f}. "
                    "Kept the champion.",
                )

    if champion is not None:
        client.set_registered_model_alias(name, PREVIOUS, champion[0])
    client.set_registered_model_alias(name, CHAMPION, version)
    client.set_model_version_tag(name, version, "promoted_at", pd.Timestamp.now("UTC").isoformat())
    previous = f" Version {champion[0]} kept as previous_champion." if champion else ""
    return Decision(True, version, f"Version {version} is now the champion.{previous}")


def rollback(profile_name: str) -> Decision:
    """Put the previous champion back in charge, keeping the current one to hand."""
    mlflow, client = _client()
    if mlflow is None:
        return Decision(False, None, "MLflow is not installed.")
    name = model_name(profile_name)
    previous = _version_tags(client, name, PREVIOUS)
    champion = _version_tags(client, name, CHAMPION)
    if previous is None:
        return Decision(False, None, "There is no previous champion to roll back to.")
    client.set_registered_model_alias(name, CHAMPION, previous[0])
    if champion is not None:
        client.set_registered_model_alias(name, PREVIOUS, champion[0])
    return Decision(
        True, previous[0],
        f"Rolled back: version {previous[0]} is the champion again"
        + (f"; version {champion[0]} is now previous_champion." if champion else "."),
    )


def status(profile_name: str) -> pd.DataFrame:
    """Every registered version, with its aliases and held-out scores."""
    mlflow, client = _client()
    if mlflow is None:
        return pd.DataFrame()
    name = model_name(profile_name)
    try:
        registered = client.get_registered_model(name)
    except Exception:
        return pd.DataFrame()
    aliases: dict[str, list[str]] = {}
    for alias, version in (registered.aliases or {}).items():
        aliases.setdefault(str(version), []).append(alias)
    rows = []
    for mv in client.search_model_versions(f"name='{name}'"):
        t = dict(mv.tags)
        rows.append(
            {
                "version": int(mv.version),
                "aliases": ", ".join(sorted(aliases.get(str(mv.version), []))),
                "trained_through": t.get("trained_through", ""),
                "passed_checks": t.get("usable", ""),
                "test_log_loss": t.get("test_log_loss", ""),
                "test_calibration_error": t.get("test_calibration_error", ""),
                "run_id": mv.run_id,
            }
        )
    return pd.DataFrame(rows).sort_values("version", ascending=False, ignore_index=True)


def load(profile_name: str, alias: str = CHAMPION):
    """The registered pipeline behind an alias, with its tags; None if unset."""
    mlflow, client = _client()
    if mlflow is None:
        return None
    import mlflow.sklearn  # noqa: PLC0415

    name = model_name(profile_name)
    found = _version_tags(client, name, alias)
    if found is None:
        return None
    version, tags = found
    pipeline = mlflow.sklearn.load_model(f"models:/{name}@{alias}")
    return version, pipeline, tags
