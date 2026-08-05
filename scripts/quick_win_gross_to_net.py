#!/usr/bin/env python3
"""
DEPRECATED (Week 1). Superseded by scripts/run_waterfall.py.

The Week 1 quick win was a gross-to-net decomposition for a single customer.
The Week 2 engine does the same thing and more, so this script now just
forwards, to avoid two implementations of the same arithmetic drifting apart:

    python scripts/run_waterfall.py --customer CUS-013

WHAT THE SCRIPT DOES, IN PLAIN TERMS
------------------------------------
Take everything a customer bought at list price, then subtract every deduction,
concession, fee and cost in turn, to see exactly how much money survives to the
bottom -- that surviving amount is the pocket margin, i.e. what the SELLER
actually keeps once all the concessions given to that customer are accounted for.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).with_name("run_waterfall.py")
    print(f"[deprecated] use: python {target.relative_to(Path.cwd()) if Path.cwd() in target.parents else target.name} --customer <ID>\n")
    sys.argv[0] = str(target)
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), {"__name__": "__main__", "__file__": str(target)})
