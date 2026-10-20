#!/usr/bin/env python3
"""Run the pricing module against one of three datasets.

    python scripts/scenarios.py            # menu
    python scripts/scenarios.py 1          # scenario 1, all four steps
    python scripts/scenarios.py 3 models   # scenario 3, one step
    python scripts/scenarios.py compare    # all three, side by side
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PYTHON = sys.executable
DELIVERED = REPO_ROOT.parent / "axiom_pricing_module_Knowledge-graph_PoC.json"
SCENARIO_DIR = REPO_ROOT / "data" / "scenarios"

# Steps run as child processes writing straight to the terminal, so this script's
# own lines have to reach it first or they land after the output they head.
if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - stream setup
    sys.stdout.reconfigure(line_buffering=True)

RULE = "=" * 78


def topup_path() -> Path | None:
    """The topped-up export, whose name carries the order count it reached."""
    found = [p for p in sorted(SCENARIO_DIR.glob("axiom_topup_*.json"))
             if not p.stem.endswith("_truth")]
    return found[-1] if found else None


def reduced_path() -> Path | None:
    """The reduced export, whichever cut was built last."""
    found = sorted(SCENARIO_DIR.glob("axiom_reduced_*.json"))
    return found[-1] if found else None


SCENARIOS: dict[str, dict] = {
    "1": {"title": "Data as it arrived", "path": lambda: DELIVERED, "build": None},
    "2": {"title": "Data reduced", "path": reduced_path,
          "build": ["make_scenario.py", "reduced"]},
    "3": {"title": "Data topped up until it passes", "path": topup_path,
          "build": ["make_scenario.py", "topup"]},
}

STEPS: list[tuple[str, str]] = [
    ("check", "check_data.py"),
    ("readiness", "run_readiness.py"),
    ("waterfall", "run_waterfall.py"),
    ("models", "run_models.py"),
]


def environment(scenario: str, path: Path) -> dict[str, str]:
    """The three variables that select a dataset and keep its output separate."""
    env = dict(os.environ)
    env["PRICING_PROFILE"] = "axiom_poc"
    env["PRICING_GRAPH_JSON"] = str(path)
    # reports/ is not keyed by profile, so without this the second scenario's
    # numbers land on top of the first one's under the same file names.
    env["PRICING_REPORTS_DIR"] = f"reports_scenario_{scenario}"
    return env


def run_step(script: str, env: dict[str, str]) -> int:
    """Run one step as its own process, so its output is exactly its own."""
    print(f"\n--- {script} " + "-" * (74 - len(script)))
    sys.stdout.flush()
    return subprocess.run(
        [PYTHON, str(REPO_ROOT / "scripts" / script)], env=env, cwd=REPO_ROOT, check=False
    ).returncode


def ensure_dataset(scenario: str) -> Path | None:
    """Find a scenario's dataset, offering to build it when it is missing."""
    spec = SCENARIOS[scenario]
    path = spec["path"]()
    if path and path.exists():
        return path
    if spec["build"] is None:
        print(f"\nThe delivered export is not at {DELIVERED}")
        return None

    print(f"\nScenario {scenario} is not built: python scripts/{' '.join(spec['build'])}")
    if input("Build it now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        subprocess.run(
            [PYTHON, str(REPO_ROOT / "scripts" / spec["build"][0]), *spec["build"][1:]],
            cwd=REPO_ROOT, check=False,
        )
        path = spec["path"]()
        if path and path.exists():
            return path
    return None


def run_scenario(scenario: str, only: str | None = None) -> int:
    """Run one scenario: all four steps, or one named step."""
    path = ensure_dataset(scenario)
    if path is None:
        return 1

    steps = [s for s in STEPS if only is None or s[0] == only]
    if not steps:
        print(f"No step called {only!r}. Steps: {', '.join(k for k, _ in STEPS)}")
        return 1

    env = environment(scenario, path)
    print()
    print(RULE)
    print(f"  SCENARIO {scenario} -- {SCENARIOS[scenario]['title']}")
    print(f"  {path.name}  ({path.stat().st_size / 1e6:,.1f} MB)")
    print(RULE)

    # A run refused at the entry gate never writes its output, so a file left by
    # an earlier successful run would be read as this run's result.
    reports = REPO_ROOT / env["PRICING_REPORTS_DIR"]
    for key, filename in (("waterfall", "pipeline_status.json"),
                          ("readiness", "readiness_capabilities.csv")):
        if any(s[0] == key for s in steps):
            (reports / filename).unlink(missing_ok=True)

    # Each script prints its own `Pipeline: PASS`/`FAIL` and exits accordingly,
    # so returning the worst exit code makes the menu gate-able too.
    exits = [run_step(script, env) for _, script in steps]
    print(f"\n{RULE}\n  reports: {env['PRICING_REPORTS_DIR']}/\n{RULE}")
    return max(exits) if exits else 0


def compare() -> int:
    """All three scenarios in one table, computed in a single process."""
    import pandas as pd

    from pricing.engine import pipeline
    from pricing.graph.json_source import GraphJsonSource
    from pricing.profiles import AXIOM_POC
    from pricing.quality.readiness import assess

    rows = []
    for key in ("1", "2", "3"):
        path = SCENARIOS[key]["path"]()
        if not path or not path.exists():
            print(f"scenario {key} is not built")
            continue
        print(f"reading scenario {key} ...")
        src = GraphJsonSource(export=path, profile=AXIOM_POC)
        ready = assess(src, AXIOM_POC)
        run = pipeline.run(src=src, profile=AXIOM_POC, track=False)
        totals, lines = run.waterfall.totals, run.waterfall.line_view()
        rows.append({
            "scenario": f"{key}. {SCENARIOS[key]['title']}",
            "orders": int(totals["n_transactions"]),
            "order lines": len(lines),
            "products": lines["product_id"].nunique(),
            "customers": lines["customer_id"].nunique(),
            "median lines per product": int(lines.groupby("product_id").size().median()),
            "currencies": lines["currency_code"].nunique(),
            "coverage %": round(float(totals["coverage_pct"]), 1),
            "readiness": ready.score,
            **dict(zip(ready.capabilities["deliverable"],
                       ready.capabilities["status"], strict=True)),
        })

    if not rows:
        return 1
    pd.set_option("display.width", 200)
    print(f"\n{RULE}")
    print(pd.DataFrame(rows).set_index("scenario").T.to_string())
    print(RULE)
    return 0


def menu() -> int:
    """The interactive loop."""
    while True:
        print(f"\n{RULE}\n  AXIOM PRICING MODULE -- SCENARIOS\n{RULE}\n")
        for key in ("1", "2", "3"):
            path = SCENARIOS[key]["path"]()
            state = "ready" if path and path.exists() else "not built"
            print(f"    {key}   {SCENARIOS[key]['title']:<34} [{state}]")
        print("\n    c   Compare all three")
        print("    b   Build the datasets for 2 and 3")
        print("    q   Quit\n")
        choice = input("  Choose: ").strip().lower()

        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "c":
            compare()
        elif choice == "b":
            for spec in (SCENARIOS["2"], SCENARIOS["3"]):
                subprocess.run(
                    [PYTHON, str(REPO_ROOT / "scripts" / spec["build"][0]),
                     *spec["build"][1:]], cwd=REPO_ROOT, check=False,
                )
        elif choice in SCENARIOS:
            print("\n  Which step?")
            for i, (_key, script) in enumerate(STEPS, 1):
                print(f"    {i}   {script}")
            print("    a   all four")
            which = input("\n  Choose [a]: ").strip().lower() or "a"
            only = STEPS[int(which) - 1][0] if which.isdigit() and 1 <= int(which) <= 4 else None
            run_scenario(choice, only=only)
            input("\n  Press Enter. ")
        else:
            print(f"\n  '{choice}' is not one of the options.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("scenario", nargs="?", help="1, 2, 3, or 'compare'")
    parser.add_argument("step", nargs="?", help=f"one of: {', '.join(k for k, _ in STEPS)}")
    args = parser.parse_args()

    if args.scenario is None:
        return menu()
    if args.scenario in ("compare", "c", "all"):
        return compare()
    if args.scenario not in SCENARIOS:
        print(f"Unknown scenario {args.scenario!r}. Use 1, 2, 3 or compare.", file=sys.stderr)
        return 1
    return run_scenario(args.scenario, only=args.step)


if __name__ == "__main__":
    raise SystemExit(main())
