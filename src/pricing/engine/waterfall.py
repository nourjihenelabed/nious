"""Price Waterfall Engine (D2): a declared step chain, computed at its declared grain."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..graph.source import Source
from ..profiles import get_profile
from ..spec import KEY, LINE, Origin, Profile, inputs_of

#: Kept as module constants for scripts and notebooks that predate profiles.
#: They describe the default profile, not the engine.
_DEFAULT = get_profile()
STEPS: tuple[str, ...] = _DEFAULT.step_names
DEDUCTIONS: tuple[str, ...] = _DEFAULT.deductions
DIMENSIONS: tuple[str, ...] = (
    "customer_id", "product_id", "segment", "channel_id", "region_id", "period",
)

GRAIN_NOTE = (
    "{step} is recorded at {grain} grain in this source; "
    "grouping by {by} would require allocating it, which nothing in the data justifies"
)


@dataclass
class Waterfall:
    """One waterfall computation: line facts, document totals, and what is missing."""

    facts: pd.DataFrame
    keys: pd.DataFrame
    totals: pd.Series
    flags: pd.DataFrame
    profile: Profile = field(default_factory=get_profile)
    snapshot: dict = field(default_factory=dict)
    reference: pd.DataFrame | None = None

    @property
    def steps(self) -> tuple[str, ...]:
        """The step chain this waterfall was computed against."""
        return self.profile.step_names

    @property
    def grain_policy(self) -> str:
        """How measures coarser than the report grain are handled."""
        from ..config import GRAIN_POLICY

        return GRAIN_POLICY

    def by_transaction(self, certified_only: bool = True) -> pd.DataFrame:
        """One row per document -- the grain every step is knowable at."""
        from .aggregation import aggregate

        return aggregate(self, self.profile.key, certified_only=certified_only)

    def line_view(self) -> pd.DataFrame:
        """`facts` with document-grain steps broadcast onto each of its lines."""
        cols = [self.profile.key, *self.profile.steps_at(KEY), *self._diagnostic_columns()]
        keys = self.keys[[c for c in dict.fromkeys(cols) if c in self.keys.columns]]
        return self.facts.merge(keys, on=self.profile.key, how="left", suffixes=("", "_key"))

    def _diagnostic_columns(self) -> list[str]:
        """Columns that count edges whose amount nobody recorded."""
        return [c for c in self.keys.columns if c.startswith("n_unknown_")]

    def availability(self) -> pd.DataFrame:
        """Per-step statement of what this book can and cannot report."""
        return step_availability(self)

    def summary(self) -> str:
        """The grand total, rendered as the familiar stepped waterfall."""
        return format_waterfall(self.totals, self.profile)


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------


def complete_sum(values: pd.Series) -> float:
    """Sum a step, or UNAVAILABLE if any contributing row is missing it."""
    return values.sum() if values.notna().all() else np.nan


def ratio(numerator, denominator, scale: float = 100.0):
    """Percentage over a Series, giving NaN rather than +/-inf on a zero denominator."""
    num = pd.to_numeric(pd.Series(numerator), errors="coerce") if not isinstance(
        numerator, pd.Series
    ) else numerator
    den = pd.to_numeric(pd.Series(denominator), errors="coerce") if not isinstance(
        denominator, pd.Series
    ) else denominator
    out = scale * num / den.where(den != 0)
    return out.replace([np.inf, -np.inf], np.nan)


def scalar_ratio(numerator, denominator, scale: float = 100.0) -> float:
    """Single-value form of `ratio`, for the grand-total series."""
    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return float("nan")
    value = scale * numerator / denominator
    return value if np.isfinite(value) else float("nan")


# ---------------------------------------------------------------------------
# walking the chain
# ---------------------------------------------------------------------------


def apply_chain(df: pd.DataFrame, profile: Profile, grain: str = LINE) -> pd.DataFrame:
    """Fill in every derived step of the chain that is knowable at `grain`."""
    steps = profile.steps
    wanted = {s.name for s in steps if grain == KEY or s.grain == LINE}

    for s in steps:
        if s.origin is Origin.MEASURE and s.name in wanted and s.name not in df.columns:
            df[s.name] = np.nan

    for i, s in enumerate(steps):
        if s.origin is not Origin.DERIVED or s.name not in wanted:
            continue
        inputs = inputs_of(steps, i)
        if len(inputs) < 2:
            df[s.name] = np.nan
            continue
        # A derived level is the level above minus the deduction between them; a derived
        # deduction is the gap between the levels either side of it.
        above, below = (steps[j].name for j in inputs)
        df[s.name] = df[above] - df[below]
    return df


def _roll_up(lines: pd.DataFrame, profile: Profile) -> pd.DataFrame:
    """Sum the line-grain measures into their document. No assumption required."""
    measures = [
        s.name for s in profile.steps
        if s.origin is Origin.MEASURE and s.grain == LINE and s.name in lines.columns
    ]
    agg: dict[str, tuple] = {m: (m, complete_sum) for m in measures}
    if profile.quantity_column and profile.quantity_column in lines.columns:
        agg[profile.quantity_column] = (profile.quantity_column, "sum")
    agg["n_lines"] = (profile.key, "size")

    rolled = lines.groupby(profile.key, dropna=False).agg(**agg)

    # Dimensions constant within a document travel up with it; line-grain ones
    # deliberately do not, because there is no single value to travel.
    key_dims = [
        d.column for d in profile.dimensions
        if d.key_safe and d.column in lines.columns and d.column != profile.key
    ]
    extra = [c for c in (profile.date_column, profile.currency_column) if c and c in lines.columns]
    carried = lines.groupby(profile.key, dropna=False)[
        list(dict.fromkeys([*key_dims, *extra]))
    ].first()
    return rolled.join(carried).reset_index()


# ---------------------------------------------------------------------------
# certification
# ---------------------------------------------------------------------------


def _certify(keys: pd.DataFrame, profile: Profile) -> pd.DataFrame:
    """Mark documents whose every step input is known, and count what is not."""
    measures = [s.name for s in profile.steps if s.origin is Origin.MEASURE]
    present = pd.Series(True, index=keys.index)
    for m in measures:
        if m in keys.columns:
            present &= keys[m].notna()
        else:
            present &= False

    unknown = [c for c in keys.columns if c.startswith("n_unknown_")]
    for col in unknown:
        present &= keys[col].fillna(0) == 0

    keys["certified"] = present
    return keys


def _generic_reason(line_view: pd.DataFrame, profile: Profile) -> pd.Series:
    """Fallback explanation for an exclusion: name the first step that is missing."""
    reason = pd.Series("", index=line_view.index)
    for s in profile.steps:
        if s.origin is not Origin.MEASURE or s.name not in line_view.columns:
            continue
        blank = (reason == "") & line_view[s.name].isna()
        reason[blank] = f"{s.name} is not recorded on this {profile.entity}"
    for col in (c for c in line_view.columns if c.startswith("n_unknown_")):
        step = col.removeprefix("n_unknown_")
        blank = (reason == "") & (line_view[col].fillna(0) > 0)
        reason[blank] = f"{step} applied with unknown amount"
    reason[(reason == "") & ~line_view["certified"]] = (
        f"another {profile.entity} in this document is incomplete"
    )
    return reason


# ---------------------------------------------------------------------------
# the computation
# ---------------------------------------------------------------------------


def build(src: Source, profile: Profile | None = None, keys: pd.DataFrame | None = None):
    """Extract, roll up and walk the chain. Returns the line and document frames."""
    profile = profile or get_profile()
    if profile.build_facts is None:
        raise ValueError(f"profile {profile.name!r} declares no extraction function")

    extract = profile.build_facts(src)
    lines = extract.lines
    if keys is not None:
        wanted = set(keys[profile.key])
        lines = lines[lines[profile.key].isin(wanted)].copy()

    lines = apply_chain(lines.copy(), profile, grain=LINE)

    documents = _roll_up(lines, profile)
    if not extract.keys.empty:
        key_cols = [c for c in extract.keys.columns if c != profile.key]
        documents = documents.merge(
            extract.keys[[profile.key, *key_cols]], on=profile.key, how="left"
        )
    documents = apply_chain(documents, profile, grain=KEY)
    documents = _certify(documents, profile)

    lines = lines.merge(
        documents[[profile.key, "certified"]], on=profile.key, how="left"
    )
    line_measures = [
        s.name for s in profile.steps if s.origin is Origin.MEASURE and s.grain == LINE
    ]
    lines["line_certified"] = (
        lines[line_measures].notna().all(axis=1) if line_measures else True
    )

    view = lines.merge(
        documents[
            [profile.key, *[c for c in documents.columns if c.startswith("n_unknown_")]]
        ],
        on=profile.key,
        how="left",
    )
    reasons = (
        profile.uncertified_reasons(view, profile)
        if profile.uncertified_reasons
        else _generic_reason(view, profile)
    )
    lines["uncertified_reason"] = np.where(
        lines["certified"].to_numpy(), "", np.asarray(reasons)
    )

    return lines, documents, extract.reference


def build_facts(src: Source, transactions: pd.DataFrame | None = None) -> pd.DataFrame:
    """The line-grain fact table on its own, for callers that only want that."""
    lines, _, _ = build(src, keys=transactions)
    return lines


def totals_from(keys: pd.DataFrame, profile: Profile, certified_only: bool = True) -> pd.Series:
    """Sum the document frame into one waterfall, with coverage stated beside it."""
    scope = keys[keys["certified"]] if certified_only else keys
    n_all = len(keys)
    n_cert = int(keys["certified"].sum())

    t = pd.Series(
        {
            s: (scope[s].sum() if s in scope.columns and scope[s].notna().all() else np.nan)
            for s in profile.step_names
        }
    )
    t["n_transactions"] = float(n_all)
    t["n_certified"] = float(n_cert)
    t["n_excluded_incomplete"] = float(n_all - n_cert)
    t["coverage_pct"] = 100.0 * n_cert / n_all if n_all else np.nan
    t["n_lines"] = float(scope["n_lines"].sum()) if "n_lines" in scope.columns else float(len(scope))
    t["pocket_margin_pct"] = scalar_ratio(t.get(profile.closing), t.get(_price_level(profile)))
    t["price_realisation_pct"] = scalar_ratio(t.get(_price_level(profile)), t.get(profile.opening))
    return t


def _price_level(profile: Profile) -> str:
    """The level margin is measured against: the last level above the closing one."""
    levels = profile.levels
    return levels[-2] if len(levels) >= 2 else levels[-1]


def step_availability(w: Waterfall) -> pd.DataFrame:
    """Per waterfall step: how much of the book can actually report it."""
    view = w.line_view()
    profile = w.profile
    n_lines = len(view)
    n_docs = len(w.keys)

    rows = []
    for s in profile.steps:
        if s.name not in view.columns:
            continue
        present = view[s.name].notna()
        whole = view.assign(_p=present).groupby(profile.key)["_p"].all()
        rows.append(
            {
                "step": s.name,
                "grain": s.grain,
                "n_lines_available": int(present.sum()),
                "n_lines_unavailable": int((~present).sum()),
                "n_transactions_available": int(whole.sum()),
                "n_transactions_unavailable": int(n_docs - whole.sum()),
                "availability_pct": round(100.0 * present.sum() / n_lines, 2) if n_lines else float("nan"),
                "n_zero": int((view[s.name] == 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _generic_flags(w: Waterfall) -> pd.DataFrame:
    """Data-quality findings any profile can produce, from the chain alone."""
    profile = w.profile
    view = w.line_view()
    rows = []
    for s in profile.steps:
        if s.origin is not Origin.MEASURE or s.name not in view.columns:
            continue
        missing = view[s.name].isna()
        if not missing.any():
            continue
        rows.append(
            {
                "flag": f"{s.name} not recorded",
                "n_lines": int(missing.sum()),
                "n_transactions": int(view.loc[missing, profile.key].nunique()),
                "consequence": f"{s.name} and every step below it UNAVAILABLE",
            }
        )
    closing = profile.closing
    if closing in w.keys.columns:
        negative = w.keys[closing] < 0
        rows.append(
            {
                "flag": f"negative {closing}",
                "n_lines": int(negative.sum()),
                "n_transactions": int(negative.sum()),
                "consequence": "selling below cost -- a real finding, not an error",
            }
        )
    return pd.DataFrame(rows)


def flags_from(w: Waterfall) -> pd.DataFrame:
    """The profile's own data-quality findings, or the generic ones."""
    return w.profile.quality_flags(w) if w.profile.quality_flags else _generic_flags(w)


def compute(
    src: Source,
    transactions: pd.DataFrame | None = None,
    profile: Profile | None = None,
) -> Waterfall:
    """Full waterfall over the snapshot, or over a subset of documents."""
    profile = profile or get_profile()
    lines, keys, reference = build(src, profile, transactions)
    w = Waterfall(
        facts=lines,
        keys=keys,
        totals=totals_from(keys, profile),
        flags=pd.DataFrame(),
        profile=profile,
        snapshot=src.snapshot_info(),
        reference=reference,
    )
    w.flags = flags_from(w)
    return w


def for_customer(src: Source, customer_id: str, profile: Profile | None = None) -> Waterfall:
    """Waterfall for one customer."""
    profile = profile or get_profile()
    dim = profile.dimension("customer")
    if dim is None:
        raise ValueError(f"profile {profile.name!r} has no customer dimension")

    lines, keys, _ = build(src, profile)
    wanted = keys[keys[dim.column] == customer_id] if dim.column in keys.columns else keys.iloc[:0]
    if wanted.empty:
        raise ValueError(f"No transactions found for customer {customer_id!r}")
    return compute(src, wanted[[profile.key]], profile)


def format_waterfall(t: pd.Series, profile: Profile | None = None, indent: str = "  ") -> str:
    """Render a totals Series as the familiar stepped waterfall."""
    profile = profile or get_profile()
    base = t.get(profile.opening)

    def money(x):
        """Right-aligned amount, or n/a when the step is unavailable."""
        return "           n/a" if pd.isna(x) else f"{x:>14,.2f}"

    def share(x):
        """The step as a percentage of the opening level, where that is knowable."""
        if pd.isna(base) or not base or pd.isna(x):
            return ""
        return f"   ({100 * x / base:5.1f}% of {profile.opening.replace('_', ' ')})"

    out = []
    if not pd.isna(t.get("coverage_pct", np.nan)):
        out += [
            f"{indent}Coverage: {int(t.get('n_certified', 0))}/{int(t.get('n_transactions', 0))} "
            f"documents fully decomposable ({t['coverage_pct']:.1f}%). Certified only below.",
            "",
        ]
    for s in profile.steps:
        out.append(f"{indent}{s.display():<28}{money(t.get(s.name))}{share(t.get(s.name))}")
    out += [
        f"{indent}{'Pocket margin %':<28}{t.get('pocket_margin_pct', float('nan')):>14.2f}%",
        f"{indent}{'Price realisation':<28}{t.get('price_realisation_pct', float('nan')):>14.2f}%"
        f"   ({_price_level(profile).replace('_', ' ')} / {profile.opening.replace('_', ' ')})",
    ]
    return "\n".join(out)
