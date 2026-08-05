"""
The engine pipeline — one function that runs the module end to end.

    validate -> waterfall -> aggregate -> contract -> track

This is the module's single entry point. Scripts, notebooks and (later) the
containerised service all call `run()`; none of them wire the steps themselves.
That is what keeps "how a result is produced" in one auditable place, which is
the practical version of the Brief's stage-gate requirement (section 7.4):

    validate inputs at the entry point and refuse to run on incomplete data;
    persist results durably; log what was computed, when, and from which
    data snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import config
from ..contracts import schema as C
from ..graph import require_valid
from ..graph.source import GraphSource, get_source
from ..graph.validation import ValidationReport
from ..tracking import log_engine_run, run_context
from . import result as result_builder
from . import waterfall as wf
from .aggregation import by_all_dimensions, reconciles_with_total

DEFAULT_GRAINS: tuple[str, ...] = ("segment", "customer", "product", "channel", "region", "period")


@dataclass
class PipelineRun:
    """Everything one run produced. Returned rather than printed."""

    waterfall: wf.Waterfall
    aggregations: dict[str, pd.DataFrame]
    result: C.EngineResult
    validation: ValidationReport
    reconciliation: pd.DataFrame
    contract_violations: list[str] = field(default_factory=list)
    mlflow_run_id: str | None = None

    @property
    def ok(self) -> bool:
        return not self.contract_violations and bool(self.reconciliation["ok"].all())

    def save(self, out_dir: Path | None = None) -> Path:
        """Persist durably, so a run can be resumed, reviewed and audited."""
        out = Path(out_dir or config.REPORTS_DIR)
        out.mkdir(parents=True, exist_ok=True)

        self.waterfall.facts.to_csv(out / "waterfall_facts.csv", index=False)
        self.waterfall.totals.to_frame("value").to_csv(out / "waterfall_totals.csv")
        self.waterfall.flags.to_csv(out / "waterfall_flags.csv", index=False)
        for name, df in self.aggregations.items():
            df.to_csv(out / f"waterfall_by_{name}.csv", index=False)
        (out / "engine_result.json").write_text(self.result.to_json(), encoding="utf-8")
        self.reconciliation.to_csv(out / "reconciliation.csv", index=False)
        (out / "validation.txt").write_text(str(self.validation), encoding="utf-8")
        if self.contract_violations:
            (out / "contract_violations.json").write_text(
                json.dumps(self.contract_violations, indent=2), encoding="utf-8"
            )
        return out


def run(
    src: GraphSource | None = None,
    grains: tuple[str, ...] = DEFAULT_GRAINS,
    contract_grains: tuple[str | list[str], ...] = (["segment", "currency"],),
    min_transactions: int = 3,
    track: bool = True,
    run_name: str = "waterfall",
) -> PipelineRun:
    """
    Run the pricing engine.

    Parameters
    ----------
    grains
        Dimensions to aggregate for reporting.
    contract_grains
        Dimensions emitted in the Track B result object. Kept narrower than
        `grains` on purpose: the contract is a handover payload, not a data
        dump, and every record in it carries evidence IDs.

        Note the default includes `currency`. The graph holds five currencies
        and no FX rate, so a plain "segment" grain spans currencies and the
        engine refuses to publish it. Splitting by currency makes every record
        a defensible number in a stated unit.
    min_transactions
        Groups thinner than this abstain rather than publishing a number from
        two deals.
    track
        Log the run to MLflow. Off for tests.
    """
    src = src or get_source()

    # -- 1. gate: refuse to run on a structurally broken snapshot ----------
    validation = require_valid(src)

    # -- 2. compute --------------------------------------------------------
    waterfall = wf.compute(src)

    # -- 3. aggregate ------------------------------------------------------
    aggregations = by_all_dimensions(waterfall, grains, min_transactions=min_transactions)

    # -- 4. self-check: aggregates must sum back to the grand total --------
    # A join fanout that double-counts is invisible in the numbers but obvious
    # here. Cheap insurance, run every time.
    reconciliation = pd.concat(
        [reconciles_with_total(waterfall, d).assign(dimension=d) for d in grains],
        ignore_index=True,
    )

    # -- 5. translate to the Track B contract ------------------------------
    engine_result = result_builder.build(
        waterfall, grains=contract_grains, min_transactions=min_transactions
    )
    violations = C.validate(engine_result)

    # -- 6. track ----------------------------------------------------------
    run_id = None
    if track:
        with run_context(run_name, tags={"phase": "week2", "stage": "waterfall"}) as active:
            log_engine_run(
                active,
                waterfall,
                aggregations,
                engine_result,
                extra_params={
                    "min_transactions": min_transactions,
                    "contract_grains": ";".join(
                        g if isinstance(g, str) else "x".join(g) for g in contract_grains
                    ),
                    "validation_warnings": len(validation.warnings),
                },
            )
            active.log_metric("reconciliation_failures", int((~reconciliation["ok"]).sum()))
            run_id = active.info.run_id

    return PipelineRun(
        waterfall=waterfall,
        aggregations=aggregations,
        result=engine_result,
        validation=validation,
        reconciliation=reconciliation,
        contract_violations=violations,
        mlflow_run_id=run_id,
    )
