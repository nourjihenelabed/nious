#!/usr/bin/env python3
"""Will this data work? Answered before anything is computed on it.

    python scripts/check_data.py                              # the configured snapshot
    python scripts/check_data.py data/snapshot_2026-09-01     # a folder you just dropped in
    python scripts/check_data.py --identify data/incoming     # which profile is this?
    python scripts/check_data.py --describe                   # what must the data look like?
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from pricing import config  # noqa: E402
from pricing.profiles import REGISTRY, get_profile  # noqa: E402
from pricing.quality.conformance import Fit, check, describe, identify  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line: what to check, against which profile."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("data_dir", nargs="?", default=None, help="directory of CSVs to check")
    p.add_argument("--profile", default=None, help=f"one of {sorted(REGISTRY)}")
    p.add_argument("--identify", action="store_true", help="rank every profile against the data")
    p.add_argument("--describe", action="store_true", help="print the schema a profile requires")
    p.add_argument("--write", action="store_true", help="also write the report to reports/")
    return p.parse_args(argv)


def _default_target(profile) -> Path:
    """What to check when the command line names nothing."""
    if not config.SOURCE_IS_EXPLICIT and getattr(profile, "default_source", None) in (
        "graph_json",
        "json",
    ):
        return config.GRAPH_JSON
    if config.SOURCE in ("graph_json", "json"):
        return config.GRAPH_JSON
    return config.CSV_DIR


def main(argv: list[str] | None = None) -> int:
    """Check, print, and return a gate-able exit code."""
    args = parse_args(argv)
    pd.set_option("display.width", 200)
    profile = get_profile(args.profile)

    if args.describe:
        print(f"Schema required by profile {profile.name!r}:\n")
        print(describe(profile).to_string(index=False))
        print(
            "\nColumns not listed above are read by the profile's extraction and are "
            "reported by a real check against actual data."
        )
        return 0

    target = Path(args.data_dir) if args.data_dir else _default_target(profile)

    if args.identify:
        print(f"Which profile does {target} look like?\n")
        print(identify(target).to_string(index=False))
        return 0

    verdict = check(target, profile)
    print(verdict)
    print()
    print(verdict.to_frame().to_string(index=False))

    if args.write:
        out = config.REPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        verdict.to_frame().to_csv(out / f"conformance_{profile.name}.csv", index=False)
        (out / f"conformance_{profile.name}.txt").write_text(str(verdict), encoding="utf-8")
        print(f"\nSaved to {out}")

    if verdict.fit is Fit.UNUSABLE:
        print("\nThis data will not run. Fix the items above, or pick another profile:")
        print(identify(target).to_string(index=False))
        return 1
    if verdict.fit is Fit.USABLE:
        print(
            "\nThis data will run, but the columns listed above will read as blank. "
            "That is a silent under-report, not an error -- fix them first."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
