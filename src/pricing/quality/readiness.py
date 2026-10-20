"""Pricing Data Readiness Score (D3): the admission test for a new dataset."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from .. import config
from ..graph.source import Source
from ..spec import KEY, LINE, Origin, Profile

#: Score at or above which a dataset is fit for the full module.
READY_AT = 80.0
#: Score below which nothing should be computed on it at all.
BLOCKED_BELOW = 40.0

#: How many documents each deliverable needs before its output means anything.
#: These are not statistical thresholds so much as honesty thresholds: below
#: them the method still returns a number, and the number is noise.
#:
#: They live in `config` now rather than here, because a threshold that can only
#: be changed by editing this file is one that gets changed by editing this file
#: -- and then nothing downstream knows it happened. Lowering one is legitimate;
#: doing it silently is not. `config.depth_reservations()` names every threshold
#: currently set below `config.RECOMMENDED_DEPTH`, and that list travels into the
#: readiness report and into the confidence reasons on every model fitted under
#: it. See `docs/WEEK4_GRAIN_AND_READINESS.md`.
DEPTH_REQUIREMENTS: dict[str, int] = dict(config.MIN_DOCUMENTS)


class Band(str, Enum):
    """How much of the module a dataset can carry."""

    READY = "ready"
    LIMITED = "limited"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Check:
    """One scored dimension of readiness, with the evidence behind it."""

    name: str
    score: float
    weight: float
    detail: str
    missing: tuple[str, ...] = ()

    @property
    def weighted(self) -> float:
        """This check's contribution to the overall score."""
        return self.score * self.weight


@dataclass
class Readiness:
    """The whole verdict: a score, the checks behind it, and what to ask for."""

    profile_name: str
    checks: tuple[Check, ...]
    capabilities: pd.DataFrame
    blocking: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Thresholds set below what the module recommends, for this run. Not a
    #: finding about the data -- a finding about the question being asked of it.
    reservations: tuple[str, ...] = field(default_factory=config.depth_reservations)

    @property
    def score(self) -> float:
        """Weighted overall score out of 100."""
        total_weight = sum(c.weight for c in self.checks) or 1.0
        return round(sum(c.weighted for c in self.checks) / total_weight, 1)

    @property
    def band(self) -> Band:
        """The verdict."""
        if self.blocking or self.score < BLOCKED_BELOW:
            return Band.BLOCKED
        if self.score < READY_AT:
            return Band.LIMITED
        unavailable = (self.capabilities["status"] == "BLOCKED").any()
        return Band.LIMITED if unavailable else Band.READY

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Every field the checks named, de-duplicated, in check order."""
        seen: dict[str, None] = {}
        for c in self.checks:
            for m in c.missing:
                seen.setdefault(m, None)
        return tuple(seen)

    def to_frame(self) -> pd.DataFrame:
        """The checks as a table, for a report or a notebook."""
        return pd.DataFrame(
            [
                {
                    "check": c.name,
                    "score": round(c.score, 1),
                    "weight": c.weight,
                    "detail": c.detail,
                    "missing": ", ".join(c.missing),
                }
                for c in self.checks
            ]
        )

    def __str__(self) -> str:
        """The verdict as printable text, shortest useful form first."""
        head = f"Pricing Data Readiness: {self.score}/100 -- {self.band.value.upper()} ({self.profile_name})"
        lines = [head, ""]
        lines += [
            f"  {CHECK_LABELS.get(c.name, c.name):<14}{c.score:>6.1f}  {c.detail}"
            for c in self.checks
        ]
        if self.blocking:
            lines += ["", "  Problems that stop the run:"]
            lines += [f"    - {b}" for b in self.blocking]
        if self.missing_fields:
            lines += ["", "  What would raise the score:"]
            lines += [f"    - {m}" for m in self.missing_fields]
        if self.reservations:
            lines += ["", "  Thresholds lowered for this run:"]
            lines += [f"    - {r}" for r in self.reservations]
            lines += [
                "    Everything fitted under these is reported with the reservation",
                "    attached. It is not the same evidence, and should not read as if",
                "    it were.",
            ]
        lines += ["", "  What this data can produce:"]
        for _, row in self.capabilities.iterrows():
            name = DELIVERABLE_LABELS.get(row["deliverable"], row["deliverable"])
            status = STATUS_LABELS.get(row["status"], row["status"])
            note = f"  -- {row['reason']}" if row["reason"] else ""
            lines.append(f"    {name:<23}{status:<17}{note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# plain names
# ---------------------------------------------------------------------------

#: What each check is called on screen. The internal names stay as they are,
#: because MLflow metrics and saved reports are keyed by them.
CHECK_LABELS = {
    "structure": "Tables",
    "chain": "Price steps",
    "grain": "Per product",
    "integrity": "Data problems",
    "depth": "History",
}
STATUS_LABELS = {"READY": "yes", "LIMITED": "yes, with limits", "BLOCKED": "no"}
DELIVERABLE_LABELS = {
    "waterfall": "Price waterfall",
    "margin_analytics": "Margin analysis",
    "segmentation": "Customer groups",
    "elasticity": "Price sensitivity",
    "optimisation": "Price recommendations",
}


def _records(profile: Profile) -> str:
    """What one document is called in this data: orders, transactions, ..."""
    return profile.key.removesuffix("_id").replace("_", " ") + "s"


def _step_name(profile: Profile, name: str) -> str:
    """A step's on-screen label, without the waterfall's +/- markers."""
    try:
        label = profile.step(name).display().lstrip("-= ").strip()
        return label[:1].lower() + label[1:]
    except KeyError:
        return name.replace("_", " ")


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def _structure_check(src: Source, profile: Profile) -> Check:
    """Do the tables the profile names exist, and do they have rows?"""
    missing, empty, rows = [], [], 0
    for logical, physical in profile.tables.items():
        try:
            df = src.table(logical)
        except Exception:
            missing.append(f"{logical} ({physical})")
            continue
        rows += len(df)
        if df.empty:
            empty.append(f"{logical} ({physical})")

    n = len(profile.tables) or 1
    score = 100.0 * (n - len(missing) - 0.5 * len(empty)) / n
    detail = f"{n - len(missing)} of {n} tables found, {rows:,} rows in total"
    if empty:
        detail += f"; {len(empty)} of them empty"
    return Check("structure", max(0.0, score), 0.20, detail, tuple(missing + empty))


def _chain_check(lines: pd.DataFrame, keys: pd.DataFrame, profile: Profile) -> Check:
    """Is every declared step actually measurable, and on how many documents?"""
    measures = [s for s in profile.steps if s.origin is Origin.MEASURE]
    if not measures:
        return Check("chain", 0.0, 0.30, "no price step is read from the data", ())

    coverages, missing = [], []
    for s in measures:
        frame = keys if s.grain == KEY else lines
        if s.name not in frame.columns:
            coverages.append(0.0)
            missing.append(f"{_step_name(profile, s.name)}: not found in the data")
            continue
        covered = float(frame[s.name].notna().mean()) if len(frame) else 0.0
        coverages.append(100.0 * covered)
        gap = round(100 * (1 - covered), 1)
        if gap:
            missing.append(f"{_step_name(profile, s.name)}: missing on {gap}% of rows")

    absent = list(profile.absent_steps)
    detail = (
        f"{len(measures)} price steps read from the data, filled in on "
        f"{np.mean(coverages):.0f}% of rows on average"
    )
    if absent:
        detail += f"; {len(absent)} usual step(s) do not exist in this data"
    return Check("chain", float(np.mean(coverages)), 0.30, detail, tuple(missing))


def _grain_check(profile: Profile) -> Check:
    """How much of the chain survives a slice by a dimension inside a document?"""
    key_steps = profile.steps_at(KEY)
    line_dims = [d for d in profile.dimensions if d.grain == LINE]
    n_steps = len(profile.steps) or 1

    if not line_dims:
        return Check(
            "grain", 100.0, 0.20,
            "every breakdown is by whole record, so every step can be shown", (),
        )

    reportable = n_steps - len(key_steps)
    score = 100.0 * reportable / n_steps
    one = _records(profile).removesuffix("s")
    breakdowns = ", ".join(d.name.replace("_", " ") for d in line_dims[:3])
    detail = (
        f"{len(key_steps)} of {n_steps} price steps are only known per {one}, so they "
        f"are left blank when you look by {breakdowns}"
    )
    missing = (
        (
            f"these amounts recorded per product line instead of per {one}: "
            + ", ".join(_step_name(profile, s) for s in key_steps)
        ),
    ) if key_steps else ()
    return Check("grain", score, 0.20, detail, missing)


def _integrity_check(report) -> Check:
    """What the entry gate found. One blocking finding is a zero, not a deduction."""
    n_block = len(report.blocking)
    n_warn = len(report.warnings)
    if n_block:
        return Check(
            "integrity", 0.0, 0.15,
            f"{n_block} problem(s) stop the run (listed below)",
            (),
        )
    score = max(0.0, 100.0 - 8.0 * n_warn)
    return Check(
        "integrity", score, 0.15, f"nothing stops the run; {n_warn} warning(s)", ()
    )


def _depth_check(keys: pd.DataFrame, profile: Profile | None = None) -> Check:
    """Is there enough history for the model deliverables, or only for the waterfall?"""
    n = len(keys)
    records = _records(profile) if profile is not None else "records"
    needed = DEPTH_REQUIREMENTS["optimisation"]
    score = 100.0 * min(1.0, n / needed)
    short = {k: v for k, v in DEPTH_REQUIREMENTS.items() if n < v}
    if not short:
        detail = f"{n:,} {records}, enough for every deliverable"
    else:
        detail = f"{n:,} {records}, not enough for: " + ", ".join(
            DELIVERABLE_LABELS.get(k, k).lower() for k in short
        )
    missing = tuple(
        f"{DELIVERABLE_LABELS.get(k, k)}: needs {v:,} {records}, has {n:,}"
        for k, v in short.items()
    )
    return Check("depth", score, 0.15, detail, missing)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def _segmentable(keys: pd.DataFrame, profile: Profile) -> tuple[bool, int, int, int]:
    """Are there enough repeat customers to cluster, by the model's own thresholds?"""
    from ..models.segmentation import DEFAULT_K, MIN_DEALS

    dimension = profile.dimension("customer")
    if dimension is None or dimension.column not in keys.columns:
        return True, 0, MIN_DEALS, DEFAULT_K
    certified = keys[keys["certified"]] if "certified" in keys.columns else keys
    per_customer = certified.groupby(dimension.column, dropna=False)[profile.key].nunique()
    n_deep = int((per_customer >= MIN_DEALS).sum())
    return n_deep >= DEFAULT_K, n_deep, MIN_DEALS, DEFAULT_K


def capabilities(
    keys: pd.DataFrame, profile: Profile, blocked: bool
) -> pd.DataFrame:
    """Per deliverable: READY, LIMITED or BLOCKED, and the reason when it is not."""
    n = len(keys)
    n_cert = int(keys["certified"].sum()) if "certified" in keys.columns else 0
    has_customer = profile.dimension("customer") is not None
    has_time = profile.date_column is not None
    has_price = profile.quantity_column is not None and profile.has(profile.opening)

    rows = []

    def add(name: str, ok: bool, limited: bool, blocked_reason: str, limited_reason: str) -> None:
        """One capability row, with BLOCKED taking precedence over everything."""
        status = "BLOCKED" if (blocked or not ok) else ("LIMITED" if limited else "READY")
        # Blocked by the data problems alone, the deliverable's own reason would
        # be false -- "needs 20, has 3,724" -- so the true reason is given.
        if status == "BLOCKED" and ok:
            blocked_reason = "stopped by the data problems listed above"
        reason = {"BLOCKED": blocked_reason, "LIMITED": limited_reason, "READY": ""}[status]
        rows.append({"deliverable": name, "status": status, "reason": reason})

    records = _records(profile)
    one = records.removesuffix("s")
    add(
        "waterfall",
        n_cert > 0,
        n_cert < n,
        f"no {one} has every price step",
        f"{n - n_cert} of {n} {records} are missing a price step and are left out",
    )
    add(
        "margin_analytics",
        n_cert >= DEPTH_REQUIREMENTS["margin_analytics"],
        len(profile.steps_at(KEY)) > 0,
        f"needs {DEPTH_REQUIREMENTS['margin_analytics']} complete {records}, has {n_cert}",
        f"per product, figures stop at the steps known only per {one}",
    )
    # Segmentation clusters customers, not documents, so document depth is not the binding
    # constraint.
    deep_enough, n_deep, min_deals, k = _segmentable(keys, profile)
    add(
        "segmentation",
        has_customer and n_cert >= DEPTH_REQUIREMENTS["segmentation"] and deep_enough,
        n_cert < 2 * DEPTH_REQUIREMENTS["segmentation"],
        "this data has no customer column"
        if not has_customer
        else (
            f"needs {DEPTH_REQUIREMENTS['segmentation']} complete {records}, has {n_cert}"
            if n_cert < DEPTH_REQUIREMENTS["segmentation"]
            else f"only {n_deep} customer(s) have {min_deals} or more {records}; "
            f"forming {k} groups needs at least {k}"
        ),
        f"{k} groups formed from {n_deep} customers with {min_deals}+ {records} each; "
        "the groups are suggestions to review, not facts",
    )
    add(
        "elasticity",
        has_time and has_price and n_cert >= DEPTH_REQUIREMENTS["elasticity"],
        n_cert < 2 * DEPTH_REQUIREMENTS["elasticity"],
        "this data has no date, so price changes cannot be put in time order"
        if not has_time
        else (
            "this data has no quantity or list price, so a price per unit cannot be worked out"
            if not has_price
            else f"needs {DEPTH_REQUIREMENTS['elasticity']} complete {records}, has {n_cert}"
        ),
        f"{n_cert} complete {records} is close to the minimum; expect wide ranges and "
        "some products left out",
    )
    add(
        "optimisation",
        has_customer and has_price and n_cert >= DEPTH_REQUIREMENTS["optimisation"],
        profile.win_data is None,
        "this data has no customer column, so there is no one to price for"
        if not has_customer
        else (
            f"needs {DEPTH_REQUIREMENTS['optimisation']} complete {records}, has {n_cert}"
            if n_cert < DEPTH_REQUIREMENTS["optimisation"]
            else "this data has no quantity or list price, so a price per unit cannot "
            "be worked out"
        ),
        "no lost quotes on record, so the chance of acceptance is estimated from won "
        "deals only",
    )
    return pd.DataFrame(rows)


def assess(src: Source, profile: Profile | None = None) -> Readiness:
    """Score a dataset against a profile, without computing anything on it."""
    from ..engine import waterfall as wf
    from ..graph import validation as V
    from ..profiles import get_profile

    profile = profile or get_profile()
    report = V.validate(src, profile)

    structure = _structure_check(src, profile)
    if structure.missing and structure.score == 0:
        empty = pd.DataFrame(columns=["certified"])
        return Readiness(
            profile_name=profile.name,
            checks=(structure,),
            capabilities=capabilities(empty, profile, blocked=True),
            blocking=(f"tables unreadable: {', '.join(structure.missing)}",),
        )

    try:
        lines, keys, _ = wf.build(src, profile)
    except Exception as exc:
        return Readiness(
            profile_name=profile.name,
            checks=(structure,),
            capabilities=capabilities(pd.DataFrame(columns=["certified"]), profile, True),
            blocking=(f"extraction failed: {exc}",),
        )

    checks = (
        structure,
        _chain_check(lines, keys, profile),
        _grain_check(profile),
        _integrity_check(report),
        _depth_check(keys, profile),
    )
    blocking = tuple(str(f) for f in report.blocking)
    return Readiness(
        profile_name=profile.name,
        checks=checks,
        capabilities=capabilities(keys, profile, blocked=bool(blocking)),
        blocking=blocking,
        warnings=tuple(str(f) for f in report.warnings),
        notes=tuple(f"{k}: {v}" for k, v in profile.absent_steps.items()),
    )
