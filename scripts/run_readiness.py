#!/usr/bin/env python3
"""Score a dataset for pricing readiness (D3) without computing anything on it.

    python scripts/run_readiness.py
    python scripts/run_readiness.py --profile dairy_al_kharj
    python scripts/run_readiness.py --all
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _env  # noqa: E402,F401  -- reads --profile/--graph/--out before pricing loads
from pricing import config  # noqa: E402
from pricing.graph import get_source  # noqa: E402
from pricing.profiles import REGISTRY, get_profile  # noqa: E402
from pricing.quality.readiness import Band, assess  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line: which profile, and whether to write the report."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--graph", help="graph export to read: a .json file or an export folder")
    p.add_argument("--out", help="folder to write reports to")
    p.add_argument("--profile", default=None, help=f"one of {sorted(REGISTRY)}")
    p.add_argument("--all", action="store_true", help="score every registered profile")
    p.add_argument("--no-write", action="store_true", help="print only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Score, print and persist the readiness verdict."""
    args = parse_args(argv)
    names = sorted(REGISTRY) if args.all else [args.profile or config.PROFILE]

    worst = Band.READY
    out = config.REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    for name in names:
        profile = get_profile(name)
        try:
            # Each profile gets the backend *it* declares, not the ambient one.
            src = get_source(profile=profile)
        except Exception as exc:
            print(f"{name}: cannot open its source -- {exc}\n", file=sys.stderr)
            worst = Band.BLOCKED
            continue

        verdict = assess(src, profile)

        # A score computed against data this profile cannot read is worse than no score, because
        # it looks like a finding.
        location = src.snapshot_info().get("location", "?")
        unreadable = next((c for c in verdict.checks if c.name == "structure"), None)
        if unreadable is not None and unreadable.missing:
            print(
                f"Pricing Data Readiness: not scored -- {name}\n\n"
                f"  {location} does not carry this profile's tables "
                f"({len(unreadable.missing)} of {len(profile.tables)} missing), so "
                "there is nothing here to score.\n"
                "  Point it at this profile's own data before reading anything into it.\n"
            )
            worst = Band.BLOCKED
            continue

        print(verdict)
        print()

        if verdict.band is Band.BLOCKED:
            worst = Band.BLOCKED
        elif verdict.band is Band.LIMITED and worst is Band.READY:
            worst = Band.LIMITED

        if args.no_write:
            continue

        verdict.to_frame().to_csv(out / f"readiness_{name}_checks.csv", index=False)
        verdict.capabilities.to_csv(out / f"readiness_{name}_capabilities.csv", index=False)
        (out / f"readiness_{name}.md").write_text(
            "\n".join(
                [
                    f"# Pricing Data Readiness -- {name}",
                    "",
                    f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
                    f"Source: `{src.snapshot_info().get('location', 'n/a')}`",
                    "",
                    "```",
                    str(verdict),
                    "```",
                    "",
                    "## Checks",
                    "",
                    verdict.to_frame().to_markdown(index=False),
                    "",
                    "## Deliverables",
                    "",
                    verdict.capabilities.to_markdown(index=False),
                ]
            ),
            encoding="utf-8",
        )
        print(f"Saved: {out / f'readiness_{name}.md'}\n")

    return 1 if worst is Band.BLOCKED else 0


if __name__ == "__main__":
    raise SystemExit(main())
