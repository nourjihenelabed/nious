"""Generate a tiny workbook with the structure a source mapping declares.

    python scripts/make_mapping_fixture.py
    python scripts/make_mapping_fixture.py --mapping ../axiom-source-mapping.json
    python scripts/make_mapping_fixture.py --out data/snapshot_dairy_tiny_v1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pricing.mapping.fixture import FixtureSpec, write  # noqa: E402
from pricing.mapping.r2rml import SourceMapping, find_mapping  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "snapshot_dairy_tiny_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line: where the mapping is, where the workbook should go."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mapping", type=Path, default=None, help="source mapping JSON")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    p.add_argument("--seed", type=int, default=20260824, help="placeholder seed")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the fixture and report what it covers."""
    args = parse_args(argv)

    path = args.mapping or find_mapping(Path.cwd())
    if path is None:
        print("No axiom-source-mapping*.json found. Pass --mapping.", file=sys.stderr)
        return 2

    mapping = SourceMapping.load(path)
    name = Path(mapping.workbook).stem or "mapped_source"
    out = args.out / f"{name}__tiny.xlsx"

    spec = FixtureSpec(mapping=mapping, out_path=out, seed=args.seed)
    written = write(spec)

    print(f"mapping   : {path}")
    print(f"format    : {mapping.format} v{mapping.artifact_version}")
    print(f"workbook  : {mapping.workbook}")
    print(f"classes   : {len(mapping.classes)} across {len(mapping.sheets)} sheets")
    print(f"written   : {written}")
    print()
    print(f"{'sheet':<22}{'rows':>6}{'cols':>6}  columns")
    from pricing.mapping.fixture import build_tables

    for sheet, df in build_tables(spec).items():
        cols = ", ".join(df.columns[:6]) + (" ..." if df.shape[1] > 6 else "")
        print(f"{sheet:<22}{len(df):>6}{df.shape[1]:>6}  {cols}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
