#!/usr/bin/env python3
"""
Run the price waterfall (D2) and its aggregations.

    python scripts/run_waterfall.py                      # whole book, all dimensions
    python scripts/run_waterfall.py --customer CUS-013   # one customer
    python scripts/run_waterfall.py --by segment         # one dimension, printed
    python scripts/run_waterfall.py --leakage customer   # where is margin leaking?
    python scripts/run_waterfall.py --no-track           # skip MLflow

Writes everything to reports/ and logs the run to MLflow.

WHAT THE WATERFALL IS, IN ONE PARAGRAPH
---------------------------------------
Start from what the goods would have cost at list price, then subtract every
deduction, concession and cost in turn -- the negotiated gap below list, the
discounts printed on the invoice, the off-invoice leakage that never appears on
it (rebates, freight, payment terms, co-op credits, side letters), and finally
the cost of the goods themselves. What survives all of that is the pocket
margin: what the SELLER actually keeps. The point of doing it step by step is
that each step is a place where money left, and you can name it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from pricing import config  # noqa: E402
from pricing.engine import aggregation as agg  # noqa: E402
from pricing.engine import pipeline, waterfall as wf  # noqa: E402
from pricing.graph import SnapshotRejected, get_source  # noqa: E402


def biggest_customer(src) -> str:
    """Fallback when no --customer is given: the largest account by revenue."""
    return src.transactions().groupby("customer_id")["gross_amount"].sum().idxmax()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", help="customer_id, e.g. CUS-013 (default: whole book)")
    ap.add_argument("--by", help="print one aggregation, e.g. segment / region / product / period")
    ap.add_argument("--leakage", metavar="DIM", help="print the leakage ranking for a dimension")
    ap.add_argument("--min-transactions", type=int, default=3,
                    help="groups thinner than this abstain instead of publishing (default 3)")
    ap.add_argument("--no-track", action="store_true", help="do not log to MLflow")
    args = ap.parse_args()

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 40)

    src = get_source()

    # ---- one customer: a quick look, no full pipeline --------------------
    if args.customer:
        w = wf.for_customer(src, args.customer)
        cust = src.customers()
        row = cust[cust["customer_id"] == args.customer]
        name = row["name"].iloc[0] if not row.empty else "?"
        seg = row["segment"].iloc[0] if not row.empty else "?"

        print("=" * 78)
        print(f"PRICE WATERFALL  |  {args.customer} - {name} ({seg})")
        print("=" * 78)
        print(w.summary())
        print("\n--- Data-quality flags ---")
        print(w.flags.to_string(index=False))
        print("\n--- By product for this customer ---")
        by_prod = agg.aggregate(w, "product")
        cols = ["product_id", "product_name", "n_transactions_certified", "list_value",
                "pocket_price", "pocket_margin", "pocket_margin_pct", "total_leakage_pct"]
        print(by_prod[[c for c in cols if c in by_prod]].to_string(index=False, float_format="%.2f"))

        out = config.REPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        w.facts.to_csv(out / f"waterfall_facts_{args.customer}.csv", index=False)
        print(f"\nSaved: {out / f'waterfall_facts_{args.customer}.csv'}")
        return 0

    # ---- whole book: the full pipeline -----------------------------------
    try:
        run = pipeline.run(src, min_transactions=args.min_transactions, track=not args.no_track)
    except SnapshotRejected as exc:
        print(exc)
        return 2

    print("=" * 78)
    print("PRICE WATERFALL  |  whole book")
    print("=" * 78)
    print(run.waterfall.summary())

    print("\n--- Snapshot validation ---")
    print(run.validation)

    print("\n--- Data-quality flags ---")
    print(run.waterfall.flags.to_string(index=False))

    print("\n--- Aggregation reconciles with the grand total? ---")
    rec = run.reconciliation.groupby("dimension")["ok"].all()
    for dim, ok in rec.items():
        print(f"  {dim:<16} {'OK' if ok else 'FAILED'}")

    if args.by:
        a = run.aggregations.get(args.by)
        if a is None:  # a dimension outside DEFAULT_GRAINS -- compute it on demand
            a = agg.aggregate(run.waterfall, args.by, min_transactions=args.min_transactions)
        print(f"\n--- Waterfall by {args.by} ---")
        cols = [c for c in a.columns if c in
                (*agg.resolve(args.by), *agg.LABEL_FOR.values(), "n_transactions",
                 "n_transactions_certified", "coverage_pct", *wf.STEPS,
                 "pocket_margin_pct", "price_realisation_pct", "status")]
        print(a[cols].to_string(index=False, float_format="%.2f"))

    if args.leakage:
        print(f"\n--- Where margin leaks, by {args.leakage} ---")
        print(agg.leakage_ranking(run.waterfall, args.leakage, 10).to_string(index=False, float_format="%.2f"))

    if run.contract_violations:
        print("\n!! CONTRACT VIOLATIONS -- result not fit to hand to Track B")
        for v in run.contract_violations:
            print(f"   {v}")
    else:
        print(f"\nResult contract: {len(run.result.waterfalls)} records, 0 violations.")

    out = run.save()
    print(f"Saved to {out}")
    if run.mlflow_run_id:
        print(f"MLflow run: {run.mlflow_run_id}")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
