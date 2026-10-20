#!/usr/bin/env python3
"""Freeze the read-only AXIOM graph into the canonical CSV snapshot, for DVC."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import config
from pricing.graph import cypher  # noqa: E402
from pricing.graph.source import Neo4jGraphSource  # noqa: E402


def export(out_dir: Path, verify: bool = False) -> dict:
    """Write every table to CSV plus a provenance manifest; nulls stay null."""
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
