#!/usr/bin/env python3
"""Week 3 / D2 gate artefact: engine vs the hand-computed reference dataset.

    python scripts/run_reference_check.py            # print + write the report
    python scripts/run_reference_check.py --quiet    # exit code only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from pricing import config  # noqa: E402
from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import waterfall as wf  # noqa: E402
from pricing.graph import CsvGraphSource  # noqa: E402

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "tests" / "reference"

STEPS = list(wf.STEPS)

KEY_COLUMN = {
    "customer": "customer_id",
    "product": "product_id",
    "segment": "segment",
    "channel": "channel_id",
    "region": "region_id",
    "period": "period",
}


def tolerance(expected: float) -> float:
    """The D2 criterion as a number: 0.01% of the value, floored at one cent."""
    return max(abs(expected) * 1e-4, 0.01)


def compare(scope: str, key: str, step: str, engine, reference) -> dict:
    """One engine-vs-reference cell, with its verdict."""
    engine_missing = pd.isna(engine)
    reference_missing = pd.isna(reference)
    if engine_missing and reference_missing:
        return {"scope": scope, "key": key, "step": step, "engine": None,
                "reference": None, "diff": None, "ok": True, "note": "UNAVAILABLE (agreed)"}
    if engine_missing or reference_missing:
        return {"scope": scope, "key": key, "step": step,
                "engine": None if engine_missing else float(engine),
                "reference": None if reference_missing else float(reference),
                "diff": None, "ok": False,
                "note": "one side UNAVAILABLE, the other is not"}
    diff = float(engine) - float(reference)
    return {"scope": scope, "key": key, "step": step, "engine": float(engine),
            "reference": float(reference), "diff": diff,
            "ok": abs(diff) <= tolerance(reference), "note": ""}


def check_transactions(w: wf.Waterfall) -> pd.DataFrame:
    """Every transaction's nine steps against expected_transactions.csv."""
    expected = pd.read_csv(REFERENCE_DIR / "expected_transactions.csv").set_index("transaction_id")
    got = w.by_transaction(certified_only=False).set_index("transaction_id")
    rows = []
    for txn_id, exp in expected.iterrows():
        for step in STEPS:
            rows.append(compare("transaction", txn_id, step, got.loc[txn_id, step], exp[step]))
    return pd.DataFrame(rows)


def check_aggregates(w: wf.Waterfall) -> pd.DataFrame:
    """Every required D2 dimension against expected_aggregates.csv."""
    expected = pd.read_csv(REFERENCE_DIR / "expected_aggregates.csv", dtype={"key": str})
    rows = []

    total = expected[expected["dimension"] == "TOTAL"].iloc[0]
    for step in STEPS:
        rows.append(compare("TOTAL", "ALL", step, w.totals[step], total[step]))

    for dimension, key_col in KEY_COLUMN.items():
        got = agg.aggregate(w, dimension).set_index(key_col)
        exp = expected[expected["dimension"] == dimension].set_index("key")
        for key, row in exp.iterrows():
            for step in STEPS:
                engine = got.loc[key, step] if key in got.index else float("nan")
                rows.append(compare(dimension, key, step, engine, row[step]))
    return pd.DataFrame(rows)


def summarise(results: pd.DataFrame) -> pd.DataFrame:
    """Pass/fail and worst observed difference, per scope."""
    return (
        results.groupby("scope")
        .agg(
            comparisons=("ok", "size"),
            failures=("ok", lambda s: int((~s).sum())),
            max_abs_diff=("diff", lambda s: s.abs().max()),
        )
        .reset_index()
    )


def as_markdown(w: wf.Waterfall, results: pd.DataFrame) -> str:
    """The reviewable artefact: totals, per-scope summary, and any failures."""
    failures = results[~results["ok"]]
    lines = [
        "# Week 3 - reference check (D2)",
        "",
        "Engine output against the hand-computed reference dataset in "
        "`tests/reference/`. Arithmetic worked out longhand in "
        "`docs/WEEK3_REFERENCE_ARITHMETIC.md`.",
        "",
        f"**Verdict: {'PASS' if failures.empty else 'FAIL'}** - "
        f"{len(results)} comparisons, {len(failures)} outside the 0.01% tolerance.",
        "",
        "## Grand total, as the engine computes it",
        "",
        "```",
        w.summary(),
        "```",
        "",
        "## Per-scope summary",
        "",
        summarise(results).to_markdown(index=False, floatfmt=".6f"),
        "",
        "## Step availability",
        "",
        "What the book can and cannot report, per waterfall step.",
        "",
        w.availability().to_markdown(index=False),
        "",
        "## Every comparison",
        "",
        results.to_markdown(index=False, floatfmt=".2f"),
        "",
    ]
    if not failures.empty:
        lines += ["## Failures", "", failures.to_markdown(index=False, floatfmt=".6f"), ""]
    return "\n".join(lines)


def main() -> int:
    """Compare every step and every dimension, write the report, return the verdict."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="write the report, print nothing")
    args = ap.parse_args()

    src = CsvGraphSource(REFERENCE_DIR / "snapshot")
    w = wf.compute(src)
    results = pd.concat([check_transactions(w), check_aggregates(w)], ignore_index=True)
    failures = results[~results["ok"]]

    out = config.REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "reference_check.md").write_text(as_markdown(w, results), encoding="utf-8")
    results.to_csv(out / "reference_check.csv", index=False)

    if not args.quiet:
        print("=" * 78)
        print("D2 REFERENCE CHECK  |  engine vs hand-computed arithmetic")
        print("=" * 78)
        print(w.summary())
        print("\n--- Step availability ---")
        print(w.availability().to_string(index=False))
        print("\n--- Comparisons ---")
        print(summarise(results).to_string(index=False))
        if failures.empty:
            print(f"\nPASS: all {len(results)} comparisons within 0.01%.")
        else:
            print(f"\nFAIL: {len(failures)} comparisons outside tolerance")
            print(failures.to_string(index=False))
        print(f"\nSaved: {out / 'reference_check.md'}")

    return 0 if failures.empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
