#!/usr/bin/env python3
"""Freeze the graph export into a CSV snapshot. Optional -- the engine reads JSON.

    python scripts/import_graph_json.py --verify
    dvc add data/snapshot_axiom_poc_v1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing import config  # noqa: E402
from pricing.graph.json_source import GraphJsonSource, NotAGraphExport, export_files  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402

DEFAULT_OUT = config.DATA_DIR / "snapshot_axiom_poc_v1"


def freeze(src: GraphJsonSource, out_dir: Path, do_verify: bool) -> dict:
    """Write every label to its own CSV, plus the edge list and a manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = src.document

    manifest: dict = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(src.export_path),
        "source_bytes": sum(f.stat().st_size for f in export_files(src.export_path)),
        "source_sha256": src.snapshot_info()["content_sha256"],
        "exported_at": doc.get("exported_at"),
        "session_id": doc.get("session_id"),
        "summary": doc.get("summary"),
        "tables": {},
    }

    for label in sorted(src.frames):
        df = src.frames[label]
        name = f"{label}.csv"
        df.to_csv(out_dir / name, index=False, na_rep="")
        manifest["tables"][label] = {
            "file": name,
            "rows": int(len(df)),
            "columns": [c for c in df.columns if c != "_node_id"],
            "null_counts": {c: int(df[c].isna().sum()) for c in df.columns},
        }
        print(f"  {name:<28} {len(df):>6,} rows  {len(df.columns) - 1} properties")

    edges = src.relationships()
    edges.to_csv(out_dir / "_relationships.csv", index=False)
    manifest["relationships"] = {
        "file": "_relationships.csv",
        "rows": int(len(edges)),
        "by_type": {k: int(v) for k, v in edges["type"].value_counts().items()},
    }
    print(f"  {'_relationships.csv':<28} {len(edges):>6,} rows")

    if do_verify:
        print("\nGraph integrity")
        findings = src.graph_integrity()
        for finding in findings:
            print(f"  !! {finding}")
        if not findings:
            print("  counts, edges, business keys and denormalised keys: all ok")
        manifest["verification"] = {
            "passed": not findings,
            "findings": [
                {"severity": f.severity.value, "check": f.check,
                 "detail": f.detail, "n_affected": f.n_affected}
                for f in findings
            ],
        }

    (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSnapshot written to {out_dir}")
    print(f"Next: dvc add {out_dir}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Read the export, freeze it, return a gate-able exit code."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", type=Path, default=config.GRAPH_JSON,
                    help="the graph export to freeze")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="snapshot directory to write")
    ap.add_argument("--profile", default="axiom_poc", help="profile to read it as")
    ap.add_argument("--verify", action="store_true",
                    help="run the graph integrity checks and record the result")
    args = ap.parse_args(argv)

    try:
        src = GraphJsonSource(args.json, profile=get_profile(args.profile))
    except (FileNotFoundError, NotAGraphExport) as exc:
        print(exc, file=sys.stderr)
        return 1

    size = sum(f.stat().st_size for f in export_files(src.export_path))
    print(f"Reading {src.export_path} ({size / 1e6:.1f} MB)\n")
    manifest = freeze(src, args.out, args.verify)

    if args.verify and not manifest["verification"]["passed"]:
        print("\nIntegrity checks failed -- this snapshot would not run anyway.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
