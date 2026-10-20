"""The engine pipeline: validate -> waterfall -> aggregate -> contract -> track."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import config
from ..analytics import margin
from ..contracts import schema as C
from ..graph import require_valid
from ..graph.source import Source, get_source
from ..graph.validation import ValidationReport
from ..profiles import get_profile
from ..quality.readiness import assess
from ..tracking import log_engine_run, run_context
from . import result as result_builder
from . import waterfall as wf
from .aggregation import by_all_dimensions, reconciles_with_total

#: Grains reported on every run of the default profile.
DEFAULT_GRAINS: tuple[str, ...] = ("segment", "customer", "product", "channel", "region", "period")


def default_grains(profile=None) -> tuple[str, ...]:
    """Grains to report for a profile: the shipped list where it applies."""
    from ..profiles import get_profile

    profile = profile or get_profile()
    declared = tuple(d.name for d in profile.dimensions if d.column != profile.key)
    supported = tuple(g for g in DEFAULT_GRAINS if profile.dimension(g))
    return supported or declared


def _default_contract_grains(profile) -> tuple[str | list[str], ...]:
    """The grain the contract is emitted at: segment by currency where both exist."""
    if profile.dimension("segment") and profile.dimension("currency"):
        return (["segment", "currency"],)
    fallback = next(
        (d.name for d in profile.dimensions if d.column != profile.key), profile.key
    )
    if profile.dimension("currency"):
        return ([fallback, "currency"],)
    return (fallback,)


@dataclass
class PipelineRun:
    """Everything one run produced, returned rather than printed."""

    waterfall: wf.Waterfall
    aggregations: dict[str, pd.DataFrame]
    result: C.EngineResult
    validation: ValidationReport
    reconciliation: pd.DataFrame
    contract_violations: list[str] = field(default_factory=list)
    mlflow_run_id: str | None = None
    readiness: object | None = None
    margin: dict[str, pd.DataFrame] = field(default_factory=dict)
    models: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the contract validates and every aggregate reconciles."""
        return not self.contract_violations and bool(self.reconciliation["ok"].all())

    def _model_frames(self) -> dict[str, pd.DataFrame]:
        """The model outputs that have a tabular form, keyed by report name."""
        frames: dict[str, pd.DataFrame] = {}
        segmentation = self.models.get("segmentation")
        if segmentation is not None:
            frames["segments"] = segmentation.assignments
            frames["segment_profile"] = segmentation.profile_table()
            if len(segmentation.selection):
                frames["segment_k_selection"] = segmentation.selection
        elasticity = self.models.get("elasticity")
        if elasticity is not None:
            frames["elasticity"] = elasticity.to_frame()
        acceptance = self.models.get("acceptance")
        if acceptance is not None:
            frames["acceptance"] = acceptance.summary()
            if getattr(acceptance, "is_win_rate", False):
                frames["win_calibration"] = acceptance.calibration()
        curves = self.models.get("tradeoff_curves")
        if curves is not None and len(curves):
            frames["tradeoff_curves"] = curves
        recommendations = self.models.get("recommendations")
        if recommendations is not None and len(recommendations):
            frames["recommendations"] = recommendations
        return frames

    def save(self, out_dir: Path | None = None) -> Path:
        """Persist every artefact durably, so a run can be resumed and audited."""
        out = Path(out_dir or config.REPORTS_DIR)
        out.mkdir(parents=True, exist_ok=True)

        self.waterfall.facts.to_csv(out / "waterfall_facts.csv", index=False)
        self.waterfall.totals.to_frame("value").to_csv(out / "waterfall_totals.csv")
        self.waterfall.flags.to_csv(out / "waterfall_flags.csv", index=False)
        self.waterfall.availability().to_csv(out / "waterfall_availability.csv", index=False)
        for name, df in self.aggregations.items():
            df.to_csv(out / f"waterfall_by_{name}.csv", index=False)
        (out / "engine_result.json").write_text(self.result.to_json(), encoding="utf-8")
        self.reconciliation.to_csv(out / "reconciliation.csv", index=False)
        (out / "validation.txt").write_text(str(self.validation), encoding="utf-8")

        if self.readiness is not None:
            (out / "readiness.txt").write_text(str(self.readiness), encoding="utf-8")
            self.readiness.to_frame().to_csv(out / "readiness_checks.csv", index=False)
            self.readiness.capabilities.to_csv(out / "readiness_capabilities.csv", index=False)
        for name, df in self.margin.items():
            if len(df):
                df.to_csv(out / f"margin_{name}.csv", index=False)
        for name, frame in self._model_frames().items():
            frame.to_csv(out / f"model_{name}.csv", index=False)
        if self.models.get("links"):
            (out / "model_links.json").write_text(
                json.dumps(self.models["links"], indent=2), encoding="utf-8"
            )
        if self.contract_violations:
            (out / "contract_violations.json").write_text(
                json.dumps(self.contract_violations, indent=2), encoding="utf-8"
            )
        return out


def run(
    src: Source | None = None,
    grains: tuple[str, ...] | None = None,
    contract_grains: tuple[str | list[str], ...] | None = None,
    min_transactions: int = 3,
    track: bool = True,
    run_name: str = "waterfall",
    with_models: bool = False,
    top_recommendations: int = 200,
    profile: object | None = None,
) -> PipelineRun:
    """Run the pricing engine end to end and return everything it produced."""
    src = src or get_source()
    profile = profile or get_profile()
    grains = grains or default_grains(profile)

    validation = require_valid(src, profile)

    waterfall = wf.compute(src, profile=profile)

    aggregations = by_all_dimensions(waterfall, grains, min_transactions=min_transactions)

    reconciliation = pd.concat(
        [reconciles_with_total(waterfall, d).assign(dimension=d) for d in grains],
        ignore_index=True,
    )

    contract_grains = contract_grains or _default_contract_grains(profile)
    engine_result = result_builder.build(
        waterfall, grains=contract_grains, min_transactions=min_transactions
    )

    readiness = assess(src, profile)
    margin_views = margin.run_all(waterfall)

    models: dict[str, object] = {}
    if with_models:
        models = fit_models(waterfall, readiness, top_recommendations, src=src)
        engine_result = _attach_models(engine_result, waterfall, models)

    violations = C.validate(engine_result)

    run_id = None
    if track:
        # The run is opened after the models have been fitted, not before, so everything the run
        # records is a finished fact.
        tags = {
            "stage": "engine",
            "profile": profile.name,
            "with_models": str(bool(with_models)),
            "dataset": Path(str(waterfall.snapshot.get("location", ""))).name or "unknown",
            "ok": str(not violations and bool(reconciliation["ok"].all())),
        }
        with run_context(run_name, tags=tags) as active:
            # The result names the run that recorded it, so a contract in Track
            # B's hands leads back to its parameters, metrics and registered model.
            if active.info.run_id:
                from dataclasses import replace

                engine_result = replace(
                    engine_result,
                    provenance=replace(engine_result.provenance, mlflow_run_id=active.info.run_id),
                )
            log_engine_run(
                active,
                waterfall,
                aggregations,
                engine_result,
                extra_params={
                    "min_transactions": min_transactions,
                    "profile": profile.name,
                    "contract_grains": ";".join(
                        g if isinstance(g, str) else "x".join(g) for g in contract_grains
                    ),
                    "validation_warnings": len(validation.warnings),
                    "with_models": bool(with_models),
                },
                readiness=readiness,
                models=models,
            )
            failures = int((~reconciliation["ok"]).sum())
            active.log_metric("reconciliation_failures", failures)
            active.log_metric("validation_blocking", len(validation.blocking))
            active.log_metric("validation_warnings", len(validation.warnings))
            # One number that answers "did this run succeed?" without reading the other thirty.
            active.log_metric("pipeline_ok", int(not violations and not failures))
            run_id = active.info.run_id

    return PipelineRun(
        waterfall=waterfall,
        aggregations=aggregations,
        result=engine_result,
        validation=validation,
        reconciliation=reconciliation,
        contract_violations=violations,
        mlflow_run_id=run_id,
        readiness=readiness,
        margin=margin_views,
        models=models,
    )


def fit_models(
    waterfall: wf.Waterfall, readiness, top_recommendations: int = 200, src: Source | None = None
) -> dict[str, object]:
    """Fit the D4-D6 deliverables the readiness score says this dataset supports."""
    from ..models import acceptance
    from ..models import elasticity as elasticity_model
    from ..models import optimiser as optimiser_model
    from ..models import segmentation as segmentation_model

    allowed = dict(
        zip(readiness.capabilities["deliverable"], readiness.capabilities["status"], strict=False)
    )
    models: dict[str, object] = {}
    skipped: dict[str, str] = {}
    reasons = dict(
        zip(readiness.capabilities["deliverable"], readiness.capabilities["reason"], strict=False)
    )

    # The chain: segments feed the elasticity and the win model; the elasticity feeds the
    # optimiser's volume rule.
    links: dict[str, str] = {}
    segmentation = None
    if allowed.get("segmentation") != "BLOCKED":
        try:
            segmentation = segmentation_model.fit(waterfall)
        except ValueError as exc:
            skipped["segmentation"] = str(exc)
        else:
            models["segmentation"] = segmentation
    else:
        skipped["segmentation"] = reasons.get("segmentation", "")

    if allowed.get("elasticity") != "BLOCKED":
        scope = config.ELASTICITY_SCOPE
        elasticity = None
        if scope == "segment" and segmentation is not None:
            try:
                elasticity = elasticity_model.fit_by_segment(waterfall, segmentation)
                links["segmentation -> elasticity"] = (
                    f"elasticity measured inside each of the {segmentation.k} segments; "
                    + (elasticity.circularity.note if elasticity.circularity else "")
                )
            except ValueError as exc:
                links["segmentation -> elasticity"] = f"not linked, fitted per product: {exc}"
        elif scope == "segment":
            links["segmentation -> elasticity"] = (
                "not linked: segmentation did not run, so elasticity is fitted per product"
            )
        if elasticity is None:
            elasticity = elasticity_model.fit(
                waterfall, scope="product" if scope == "segment" else scope
            )
        models["elasticity"] = elasticity
    else:
        skipped["elasticity"] = reasons.get("elasticity", "")

    profile = waterfall.profile
    if allowed.get("optimisation") != "BLOCKED" and profile.win_data and src is not None:
        # Won and lost quotes are on record: fit the real win-probability model
        # and optimise expected profit against it.
        from ..models import win_probability

        data = profile.win_data(src)
        if segmentation is not None:
            data = win_probability.attach_segments(data, segmentation.label_of())
            links["segmentation -> win probability"] = (
                "segment and price x segment are inputs to the win model"
            )
        else:
            links["segmentation -> win probability"] = "not linked: segmentation did not run"
        try:
            model = win_probability.fit(data)
        except ValueError as exc:
            skipped["optimisation"] = f"win-probability model not fitted: {exc}"
        else:
            references = profile.reference_prices(src)
            elasticity = models.get("elasticity")
            recommendations, curves = optimiser_model.recommend_book_with_win_model(
                waterfall, model, references, data.frame, top=top_recommendations,
                min_win_probability=config.MIN_WIN_PROBABILITY,
                margin_floor=config.MARGIN_FLOOR,
                elasticity=elasticity,
                min_volume_share=config.MIN_VOLUME_SHARE,
            )
            links["elasticity -> optimiser"] = (
                f"volume rule: expected volume at least {100 * config.MIN_VOLUME_SHARE:.0f}% "
                "of today's by the segment elasticity"
                if elasticity is not None and elasticity.any_fit_for_optimisation
                else optimiser_model.volume_rule_for(elasticity, "", "").skipped
            )
            models.update(
                acceptance=model,
                win_data=data,
                references=references,
                recommendations=recommendations,
                tradeoff_curves=curves,
            )
    elif allowed.get("optimisation") != "BLOCKED":
        acceptance = acceptance.fit(waterfall, scope=config.ACCEPTANCE_SCOPE)
        models["acceptance"] = acceptance
        models["recommendations"] = optimiser_model.recommend_book(
            waterfall, acceptance, top=top_recommendations
        )
    else:
        skipped["optimisation"] = reasons.get("optimisation", "")

    models["skipped"] = skipped
    models["links"] = links
    return models


def _attach_models(
    result: C.EngineResult, waterfall: wf.Waterfall, models: dict[str, object]
) -> C.EngineResult:
    """Fold the model outputs into the single result object Track B receives."""
    from dataclasses import replace

    segmentation = models.get("segmentation")
    elasticity = models.get("elasticity")
    acceptance = models.get("acceptance")
    recommendations = models.get("recommendations")

    records: tuple[C.PriceRecommendation, ...] = ()
    if recommendations is not None and len(recommendations):
        currency = _currency_of(waterfall)
        is_win_rate = bool(getattr(acceptance, "is_win_rate", False))
        from ..models.optimiser import Recommendation

        joined = ("constraints", "confidence_reasons", "evidence_ids")
        records = tuple(
            Recommendation(
                **{
                    k: (None if isinstance(v, float) and pd.isna(v) and k == "confidence_score" else v)
                    for k, v in row.items()
                    if k not in joined
                },
                **{
                    k: tuple(c for c in str(row[k]).split("; ") if c)
                    for k in joined
                    if k in row
                },
            ).to_contract(currency, is_win_rate=is_win_rate)
            for _, row in recommendations.iterrows()
        )

    return replace(
        result,
        segments=segmentation.to_contract(waterfall) if segmentation is not None else (),
        elasticities=elasticity.to_contract() if elasticity is not None else (),
        recommendations=records,
    )


def _currency_of(waterfall: wf.Waterfall) -> str:
    """The book's currency, or MIXED where it spans more than one."""
    col = waterfall.profile.currency_column
    if not col or col not in waterfall.keys.columns:
        return "EUR"
    codes = sorted(waterfall.keys[col].dropna().unique())
    return codes[0] if len(codes) == 1 else "MIXED"
