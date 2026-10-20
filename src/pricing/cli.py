"""Console entry points, so a host project can call the module without the scripts/ folder."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _run(script: str) -> int:
    """Execute one script in scripts/ as if it had been invoked directly."""
    target = _SCRIPTS / script
    sys.argv[0] = str(target)
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exit_code:
        return int(exit_code.code or 0)
    return 0


def waterfall() -> int:
    """axiom-waterfall — run the price waterfall and its aggregations."""
    return _run("run_waterfall.py")


def profile() -> int:
    """axiom-profile — profile the snapshot into reports/."""
    return _run("run_profile.py")


def reference_check() -> int:
    """axiom-verify — check the engine against the hand-computed reference."""
    return _run("run_reference_check.py")


def readiness() -> int:
    """axiom-readiness — score a dataset before computing anything on it."""
    return _run("run_readiness.py")


def models() -> int:
    """axiom-models — fit segmentation, elasticity and price recommendations."""
    return _run("run_models.py")


def mapping_fixture() -> int:
    """axiom-fixture — generate a workbook with a source mapping's structure."""
    return _run("make_mapping_fixture.py")


def win_model() -> int:
    """axiom-win-model — fit the win-probability model and show its report card."""
    return _run("run_win_model.py")


def monitor() -> int:
    """axiom-monitor — compare the champion win model with what has happened since."""
    return _run("run_monitoring.py")


def manage_models() -> int:
    """axiom-registry — see, promote and roll back registered win models."""
    return _run("manage_models.py")
