#!/usr/bin/env python3
"""
Run the full data profile and write it to reports/.

    python scripts/run_profile.py

Outputs:
    reports/profile_<section>.csv   -- one CSV per profile section
    reports/profile_report.md       -- readable summary (the Week 1 artefact)

Works against whichever backend PRICING_SOURCE selects, so the same command
will profile the real Neo4j graph the day we get credentials.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from pricing import config
from pricing.quality import profiling  # noqa: E402
from pricing.graph import get_source  # noqa: E402


def main() -> None:
    src = get_source()
    print(f"Profiling via '{src.name}' backend ...")
    results = profiling.run_all(src)
    info = src.snapshot_info()

    out = config.REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    for name, df in results.items():
        df.to_csv(out / f"profile_{name}.csv", index=False)

    md = [
        "# Week 1 - Graph profiling report",
        "",
        f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"Backend: `{info['backend']}`  ",
        f"Source: `{info['location']}`  ",
        f"Snapshot hash: `{info.get('content_sha256', 'n/a')[:16]}`",
        "",
        "Regenerate with `python scripts/run_profile.py`.",
        "",
    ]
    titles = {
        "table_counts": "1. Table shapes",
        "referential_integrity": "2. Referential integrity (dangling edges)",
        "null_summary": "3. Columns containing nulls",
        "amount_reconciliation": "4. What do gross_amount and net_amount mean?",
        "list_price_coverage": "5. List-price / cost coverage (can we build a waterfall?)",
        "discount_coverage": "6. Discount usage and value",
        "numeric_profile": "7. Numeric distributions",
        "categorical_profile": "8. Categorical levels",
        "date_profile": "9. Transactions over time",
    }
    for key, title in titles.items():
        df = results[key]
        md += [f"## {title}", "", df.to_markdown(index=False, floatfmt=",.2f"), ""]

    (out / "profile_report.md").write_text("\n".join(md), encoding="utf-8")

    # Console summary so the run is useful without opening a file
    pd.set_option("display.width", 140)
    print("\n--- Table shapes ---")
    print(results["table_counts"].to_string(index=False))
    print("\n--- Referential integrity ---")
    print(results["referential_integrity"].to_string(index=False))
    print("\n--- Columns with nulls ---")
    print(results["null_summary"].to_string(index=False))
    print("\n--- Amount reconciliation ---")
    print(results["amount_reconciliation"].to_string(index=False))
    print(f"\nWritten to {out}")


if __name__ == "__main__":
    main()
