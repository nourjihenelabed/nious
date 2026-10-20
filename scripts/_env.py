"""Shared command-line options that must take effect before `pricing` is imported.

    python scripts/run_models.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_early = argparse.ArgumentParser(add_help=False)
_early.add_argument("--profile")
_early.add_argument("--graph")
_early.add_argument("--out")
_known, _ = _early.parse_known_args(sys.argv[1:])

if _known.profile:
    os.environ["PRICING_PROFILE"] = _known.profile
if _known.graph:
    os.environ["PRICING_GRAPH_JSON"] = str(Path(_known.graph).resolve())
if _known.out:
    os.environ["PRICING_REPORTS_DIR"] = str(Path(_known.out).resolve())


def add_common(parser: argparse.ArgumentParser) -> None:
    """Declare the shared options on a script's own parser, so --help lists them."""
    parser.add_argument("--profile", help="which data schema to read, e.g. axiom_v2")
    parser.add_argument("--graph", help="graph export to read: a .json file or an export folder")
    parser.add_argument("--out", help="folder to write reports to")
