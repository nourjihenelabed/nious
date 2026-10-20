#!/usr/bin/env python3
"""Check the champion win model against what has happened since it was trained.

    python scripts/run_monitoring.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph
    python scripts/run_monitoring.py --profile axiom_v2 --graph ... --backtest
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import _env  # noqa: E402,F401
from pricing import config  # noqa: E402
from pricing.graph import get_source  # noqa: E402
from pricing.models import win_probability  # noqa: E402
from pricing.monitoring import monitor  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402
from pricing.tracking import registry, run_context  # noqa: E402


def customer_segments(src, profile) -> dict[str, str]:
    """Customer -> segment label, computed the way the engine computes it."""
    from pricing.engine import waterfall as wf
    from pricing.models import segmentation

    try:
        return segmentation.fit(wf.compute(src, profile=profile)).label_of()
    except ValueError as exc:
        print(f"[monitor] no segments ({exc}); the win model is monitored without them")
        return {}


def main(argv: list[str] | None = None) -> int:
    """Monitor, print, write, and log."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    _env.add_common(p)
    p.add_argument("--backtest", action="store_true",
                   help="monitor the later 20%% of this snapshot with a model trained on the rest")
    p.add_argument("--no-track", action="store_true", help="do not log to MLflow")
    p.add_argument("--report-only", action="store_true",
                   help="exit 0 even when a refresh is needed (for pipelines that only report)")
    args = p.parse_args(argv)

    profile = get_profile()
    if profile.win_data is None:
        print(f"Profile {profile.name} records no won and lost quotes; there is no win model to monitor.")
        return 1
    src = get_source(profile=profile)
    data = profile.win_data(src)
    # The win model takes the customer's segment as an input, so the monitor
    # must give it the same segments the engine computed.
    segment_of = customer_segments(src, profile)
    if segment_of:
        data = win_probability.attach_segments(data, segment_of)
    priced = win_probability._prepare(data.priced, tuple(data.categorical), tuple(data.numeric))

    if args.backtest:
        model = win_probability.fit(data)
        cutoff = pd.Timestamp(model.holdout_cutoff)
        pipeline, numeric, categorical = model.holdout_pipeline, model.numeric, model.categorical
        trained_through, source = model.holdout_cutoff, "back-test (model trained on the earlier 80%)"
    else:
        loaded = registry.load(profile.name)
        if loaded is None:
            print(f"No champion model registered for {profile.name}.")
            print("Fit one (run_models.py), promote it (manage_models.py promote), then monitor.")
            print("Or see the monitor work on this snapshot with --backtest.")
            return 1
        version, pipeline, tags = loaded
        numeric = tuple(tags["numeric_features"].split(","))
        categorical = tuple(tags["categorical_features"].split(","))
        missing = [c for c in (*numeric, *categorical) if c not in priced.columns]
        if missing:
            print(f"The champion expects inputs this data does not have: {', '.join(missing)}.")
            print("Refit it (run_models.py) and promote the new version, then monitor.")
            return 1
        trained_through = tags["trained_through"]
        cutoff = pd.Timestamp(trained_through)
        source = f"champion, version {version}"

    columns = [*numeric, *categorical]
    training = priced[priced["decided_at"] <= cutoff]
    recent = priced[priced["decided_at"] > cutoff]

    report = monitor(
        lambda f: pipeline.predict_proba(f[columns])[:, 1],
        training, recent, tuple(columns), trained_through,
    )
    print(f"Model: {source}\n")
    print(report.text())

    out = config.REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "monitoring_report.txt").write_text(report.text(), encoding="utf-8")
    if len(report.monthly):
        report.monthly.to_csv(out / "monitoring_monthly.csv", index=False)
        report.stability.to_csv(out / "monitoring_stability.csv", index=False)

    if not args.no_track:
        with run_context("monitoring", tags={"stage": "monitoring", "profile": profile.name,
                                             "model": source}) as run:
            for key, value in report.overall.items():
                run.log_metric(f"monitor__{key}", value)
            run.log_metric("monitor__needs_refresh", int(report.needs_refresh))
            for _, row in report.stability.iterrows():
                run.log_metric(f"monitor_psi__{row['input']}", row["psi"])
            for _, row in report.monthly.iterrows():
                run.log_metric(f"monitor_gap__{row['month']}",
                               row["actual_win_rate"] - row["predicted_win_rate"])
            run.log_dict({"report": report.text()}, "monitoring_report.json")
            run.set_tag("needs_refresh", str(report.needs_refresh))

    return 3 if report.needs_refresh and not args.report_only else 0


if __name__ == "__main__":
    raise SystemExit(main())
