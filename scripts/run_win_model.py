#!/usr/bin/env python3
"""Fit the win-probability model on its own and show how good it is.

    python scripts/run_win_model.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import _env  # noqa: E402,F401
from pricing import config  # noqa: E402
from pricing.graph import get_source  # noqa: E402
from pricing.models import win_probability  # noqa: E402
from pricing.profiles import get_profile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Fit, report, write."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    _env.add_common(p)
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)

    profile = get_profile()
    if profile.win_data is None:
        print(f"Profile {profile.name} records no won and lost quotes, so no win model can be fitted.")
        return 1
    data = profile.win_data(get_source(profile=profile))
    try:
        model = win_probability.fit(data)
    except ValueError as exc:
        print(f"Win-probability model not fitted: {exc}")
        return 1

    pd.set_option("display.width", 200)
    print(model.report())

    print("\nCalibration on the test deals (each band holds the same number of quotes):")
    print(model.calibration().to_string(index=False))

    # The curve averaged over every deal the model learned from: the book-wide
    # answer to "what happens to our win rate if we quote X% above baseline?"
    low, high = model.position_range
    positions = np.round(np.linspace(low, high, 7), 4)
    win = model.curve(data.priced, positions)
    curve = pd.DataFrame(
        {"price vs reference": [f"{100 * x:+.0f}%" for x in positions],
         "chance of winning": [f"{100 * w:.1f}%" for w in win]}
    )
    print("\nChance of winning at each price, averaged over past deals:")
    print(curve.to_string(index=False))

    if not args.no_write:
        out = config.REPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        (out / "model_win_report.txt").write_text(model.report(), encoding="utf-8")
        model.calibration().to_csv(out / "model_win_calibration.csv", index=False)
        print(f"\nSaved to {out}")
    return 0 if model.usable else 2


if __name__ == "__main__":
    raise SystemExit(main())
