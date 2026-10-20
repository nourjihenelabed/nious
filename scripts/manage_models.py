#!/usr/bin/env python3
"""See, promote and roll back the registered win-probability models.

    python scripts/manage_models.py status   --profile axiom_v2
    python scripts/manage_models.py promote  --profile axiom_v2      # newest candidate
    python scripts/manage_models.py promote  --profile axiom_v2 --version 3
    python scripts/manage_models.py rollback --profile axiom_v2
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
from pricing.tracking import registry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Run one registry command."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    _env.add_common(p)
    p.add_argument("command", choices=("status", "promote", "rollback"))
    p.add_argument("--version", help="promote this version instead of the newest candidate")
    p.add_argument("--force", action="store_true", help="promote without comparing")
    args = p.parse_args(argv)
    profile = args.profile or config.PROFILE

    if args.command == "status":
        table = registry.status(profile)
        if table.empty:
            print(f"No win-probability model registered for {profile} yet.")
            print("Fit one with tracking on: python scripts/run_models.py --profile", profile)
            return 0
        pd.set_option("display.width", 200)
        print(f"Registered versions of {registry.model_name(profile)}:\n")
        print(table.to_string(index=False))
        return 0

    if args.command == "promote":
        decision = registry.promote(profile, version=args.version, force=args.force)
    else:
        decision = registry.rollback(profile)
    print(decision.message)
    return 0 if decision.promoted else 1


if __name__ == "__main__":
    raise SystemExit(main())
