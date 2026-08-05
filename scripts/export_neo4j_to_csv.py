#!/usr/bin/env python3
"""
Export the read-only AXIOM knowledge graph to the canonical CSV snapshot.

WHY THIS EXISTS
---------------
Week 1 runs against a synthetic CSV placeholder because we have no Neo4j
credentials yet. This script is the bridge: once NIOUS hands over the URI, run
it once and the *same* CSV filenames and column headers appear -- so every
notebook, test and module written against the placeholder keeps working
unchanged, now on real graph data.

It is also useful after we have credentials, for two reasons the Brief cares
about (section 7.4 / MLOps requirements):
  1. Reproducibility -- a run must be regenerable from a *frozen* snapshot, not
     from a live database that may have moved underneath us.
  2. DVC -- `dvc add data/snapshot/` versions these CSVs, giving every model run
     a hash-identified input.

USAGE
-----
    # 1. put credentials in .env
    #    NEO4J_URI=bolt://host:7687
    #    NEO4J_USER=neo4j
    #    NEO4J_PASSWORD=...
    # 2. run
    python scripts/export_neo4j_to_csv.py --out data/snapshot_2026-07-08

    # optional: verify the export reconciles against the live graph
    python scripts/export_neo4j_to_csv.py --out ../snapshot --verify

GUARANTEES
----------
* Read-only. Uses Neo4j READ access mode and the queries in src/pricing/cypher.py;
  no write clause exists anywhere in the path.
* Lossless for our purposes. Node properties and the *edge* properties that carry
  pricing meaning (CONTAINS.quantity/unit_price/line_total,
  DISCOUNTED_BY.discount_amount_applied) are all preserved.
* Missing values stay missing. Empty cells are written empty, never as 0 --
  the waterfall must be able to tell "no freight allowance" from "unknown".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# make `src` importable when running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import config
from pricing.graph import cypher  # noqa: E402
from pricing.graph.source import Neo4jGraphSource  # noqa: E402


def export(out_dir: Path, verify: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Neo4jGraphSource()

    manifest: dict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_uri": src.settings.uri,
        "database": src.settings.database,
        "tables": {},
    }

    try:
        for table, filename in config.CSV_FILES.items():
            df = src.run(cypher.QUERIES[table])
            path = out_dir / filename
            # index=False  -> no phantom column
            # na_rep=""    -> NULL stays NULL; never coerced to 0
            df.to_csv(path, index=False, na_rep="")
            manifest["tables"][table] = {
                "file": filename,
                "rows": int(len(df)),
                "columns": list(df.columns),
                "null_counts": {c: int(df[c].isna().sum()) for c in df.columns},
            }
            print(f"  {filename:<28} {len(df):>7,} rows")

        if verify:
            manifest["verification"] = _verify(src, manifest)

        # A snapshot without provenance is not auditable. Always write this.
        (out_dir / "_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        print(f"\nSnapshot written to {out_dir}")
        print("Next: `dvc add` this directory to version it against your MLflow runs.")
    finally:
        src.close()

    return manifest


def _verify(src: Neo4jGraphSource, manifest: dict) -> dict:
    """Cross-check exported row counts against graph-level counts."""
    nodes = src.run(cypher.NODE_COUNTS).set_index("label")["n"].to_dict()
    rels = src.run(cypher.RELATIONSHIP_COUNTS).set_index("relationship")["n"].to_dict()
    orphans = int(src.run(cypher.ORPHAN_TRANSACTIONS).iloc[0, 0])

    expected = {
        "transactions": nodes.get("Transaction"),
        "customers": nodes.get("Customer"),
        "products": nodes.get("Product"),
        "discounts": nodes.get("Discount"),
        "channels": nodes.get("Channel"),
        "regions": nodes.get("Region"),
        "transaction_lines": rels.get("CONTAINS"),
        "transaction_discounts": rels.get("DISCOUNTED_BY"),
    }

    checks = {}
    for table, exp in expected.items():
        got = manifest["tables"].get(table, {}).get("rows")
        checks[table] = {"graph": exp, "exported": got, "match": exp == got}
        if exp != got:
            print(f"  !! MISMATCH {table}: graph={exp} exported={got}")

    if orphans:
        print(f"  !! {orphans} transactions have no PLACED_BY customer -- flag to mentor")

    return {"row_counts": checks, "orphan_transactions": orphans}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=config.DATA_DIR / f"snapshot_{datetime.now():%Y-%m-%d}",
        help="directory to write the CSV snapshot into",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="cross-check exported row counts against graph node/relationship counts",
    )
    args = ap.parse_args()
    export(args.out, verify=args.verify)
