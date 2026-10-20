#!/usr/bin/env python3
"""DEPRECATED (Week 1). Forwards to `run_waterfall.py --customer <ID>`."""

import sys
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).with_name("run_waterfall.py")
    print(f"[deprecated] use: python {target.relative_to(Path.cwd()) if Path.cwd() in target.parents else target.name} --customer <ID>\n")
    sys.argv[0] = str(target)
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), {"__name__": "__main__", "__file__": str(target)})
