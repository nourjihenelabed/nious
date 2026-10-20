#!/usr/bin/env python3
"""Fit and print the Week 5-6 deliverables: segmentation, elasticity, optimisation.

    python scripts/run_models.py                  # everything the data supports
    python scripts/run_models.py --only elasticity
    python scripts/run_models.py --top 50         # fewer price recommendations
    python scripts/run_models.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import _env  # noqa: E402,F401  -- reads --profile/--graph/--out before pricing loads
from pricing import config  # noqa: E402
from pricing.analytics import margin  # noqa: E402
from pricing.engine import pipeline  # noqa: E402
from pricing.graph import SnapshotRejected, get_source  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402

CHOICES = ("margin", "segmentation", "elasticity", "optimisation")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line: which deliverables, and how many recommendations."""
    p = argparse.ArgumentParser(description=__doc__)
    _env.add_common(p)
    p.add_argument("--only", choices=CHOICES, default=None)
    p.add_argument("--top", type=int, default=200, help="customer-product pairs to quote")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--no-track", action="store_true", help="do not log to MLflow")
    return p.parse_args(argv)


def _section(title: str) -> None:
    """A printed section header."""
    print(f"\n--- {title} ---")


def main(argv: list[str] | None = None) -> int:
    """Fit whatever the data supports, print it, and write it to reports/."""
    args = parse_args(argv)
    pd.set_option("display.width", 200)

    profile = get_profile()
    src = get_source()
    # Everything goes through the one entry point, rather than reaching past it to `wf.compute`
    # and `fit_models` directly.
    try:
        run = pipeline.run(
            src,
            profile=profile,
            with_models=True,
            top_recommendations=args.top,
            track=not args.no_track,
            run_name="models",
        )
    except SnapshotRejected as exc:
        print(exc)
        return 2

    w, verdict, models = run.waterfall, run.readiness, run.models

    print(f"Profile: {profile.name} -- {profile.description}")
    print(f"Readiness: {verdict.score}/100 ({verdict.band.value})")

    wanted = {args.only} if args.only else set(CHOICES)
    out = config.REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    if "margin" in wanted:
        views = margin.run_all(w)
        _section("Margin summary")
        print(views["summary"].to_string(index=False))
        _section("Leakage bridge")
        print(views["leakage_bridge"].to_string(index=False))
        _section("Margin quadrants")
        print(views["margin_quadrants"].head(6).to_string(index=False))
        _section("Widest realised price bands")
        print(views["price_band"].head(8).to_string(index=False))
        if not args.no_write:
            for name, df in views.items():
                if len(df):
                    df.to_csv(out / f"margin_{name}.csv", index=False)

    for name, reason in (models.get("skipped") or {}).items():
        print(f"\n[skipped] {name}: {reason}")

    seg = models.get("segmentation")
    if seg is not None and "segmentation" in wanted:
        from pricing.models.segmentation import structure_reading

        _section(f"Segments ({seg.k} clusters, {seg.n_abstained} customers abstained)")
        print(f"  How k was chosen: {seg.selection_note}")
        print(f"  Silhouette {seg.silhouette}: {structure_reading(seg.silhouette)}")
        print(f"  K-means and hierarchical agree on {seg.method_agreement} (adjusted Rand; 1 = same groups)")
        if len(seg.selection):
            print()
            print(seg.selection.to_string(index=False))
        print()
        print(seg.profile_table().to_string(index=False))
        if not args.no_write:
            seg.assignments.to_csv(out / "model_segments.csv", index=False)
            seg.profile_table().to_csv(out / "model_segment_profile.csv", index=False)
            if len(seg.selection):
                seg.selection.to_csv(out / "model_segment_k_selection.csv", index=False)

    el = models.get("elasticity")
    if el is not None and "elasticity" in wanted:
        _section(
            f"Elasticity ({el.n_scopes_fitted} fitted, {el.n_scopes_abstained} abstained; "
            f"prior {el.prior_mean:.2f} +/- {el.prior_sd:.2f}; price from {el.price_source})"
        )
        print(el.to_frame().head(12).to_string(index=False))
        usable = sum(1 for e in el.estimates if e.fit_for_optimisation)
        print(f"\nFit for optimisation: {usable} of {len(el.estimates)} scopes.")
        if not args.no_write:
            el.to_frame().to_csv(out / "model_elasticity.csv", index=False)

    acceptance = models.get("acceptance")
    recs = models.get("recommendations")
    if acceptance is not None and "optimisation" in wanted:
        if acceptance.is_win_rate:
            print()
            print(acceptance.report())
        else:
            _section("Chance of acceptance (from won deals only)")
            for note in acceptance.notes:
                print(f"  note: {note}")

        if recs is not None and len(recs):
            # A per-customer, per-product price is a thing to act on one at a time, not to read
            # down a terminal.
            complete = recs[recs["status"] == "complete"]
            _section(f"Price recommendations ({len(complete)} of {len(recs)} pairs priced)")
            print(f"  {len(complete)} customer-product pairs have a recommended price range.")
            not_priced = recs[recs["status"] != "complete"]
            if len(not_priced):
                print(f"  {len(not_priced)} were not priced:")
                for note, n in not_priced["note"].value_counts().items():
                    print(f"    {n:>4}  {note}")
            print("  Written to model_recommendations.csv -- one row per pair, with the")
            print("  range, what the pair pays today, and the evidence behind it.")
            curves = models.get("tradeoff_curves")
            if curves is not None and len(curves):
                print("  Price / chance-of-winning / profit curves: model_tradeoff_curves.csv")

            from pricing.models.optimiser import opportunity

            _section("Opportunity -- what those recommendations add up to")
            print(opportunity(recs).to_string(index=False))
            if not args.no_write:
                recs.to_csv(out / "model_recommendations.csv", index=False)
                acceptance.summary().to_csv(out / "model_acceptance.csv", index=False)
                if acceptance.is_win_rate:
                    acceptance.calibration().to_csv(out / "model_win_calibration.csv", index=False)
                    (out / "model_win_report.txt").write_text(acceptance.report(), encoding="utf-8")
                if curves is not None and len(curves):
                    curves.to_csv(out / "model_tradeoff_curves.csv", index=False)

    links = models.get("links") or {}
    if links:
        _section("How the stages feed each other")
        for name, text in links.items():
            print(f"  {name}: {text}")

    if not args.no_write:
        # The contract Track B reads, with the models folded in.
        (out / "engine_result.json").write_text(run.result.to_json(), encoding="utf-8")
        if links:
            (out / "model_links.json").write_text(json.dumps(links, indent=2), encoding="utf-8")
        if run.contract_violations:
            print(f"\nContract violations: {run.contract_violations}")
        print(f"\nSaved to {out} (engine_result.json carries the models)")
    if run.mlflow_run_id:
        print(f"MLflow run: {run.mlflow_run_id}")

    print(f"\nPipeline: {'PASS' if run.ok else 'FAIL'}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
