"""Drift monitoring (D7): is the win model still right, and are deals still alike?"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: PSI above which an input counts as having shifted majorly. CONVENTION -- the
#: credit-scoring rule of thumb (Siddiqi, Credit Risk Scorecards, 2006).
PSI_MAJOR = 0.25
PSI_MODERATE = 0.10
#: Months, latest first, read as "recent" for the refresh rule.
RECENT_MONTHS = 3
#: z-score beyond which a predicted/actual gap is more than chance.
Z_FLAG = 1.96
#: Deals in a month below which its gap is reported but never flagged.
MIN_DEALS_PER_MONTH = 20


def psi(expected: pd.Series, actual: pd.Series, bins: int = 10) -> float:
    """Population stability index between a training sample and a new one."""
    expected, actual = expected.dropna(), actual.dropna()
    if expected.empty or actual.empty:
        return float("nan")
    if pd.api.types.is_numeric_dtype(expected) and expected.nunique() > bins:
        edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
        edges[0], edges[-1] = -np.inf, np.inf
        e = pd.cut(expected, edges).value_counts(normalize=True, sort=False)
        a = pd.cut(actual, edges).value_counts(normalize=True, sort=False)
    else:
        e = expected.astype(str).value_counts(normalize=True)
        a = actual.astype(str).value_counts(normalize=True)
        e, a = e.align(a, fill_value=0.0)
    e, a = e.clip(lower=1e-4), a.clip(lower=1e-4)
    return float(((a - e) * np.log(a / e)).sum())


@dataclass
class MonitoringReport:
    """Monthly calibration, input stability, and whether to refresh."""

    monthly: pd.DataFrame
    stability: pd.DataFrame
    overall: dict[str, float]
    trained_through: str
    refresh: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_refresh(self) -> bool:
        """True when the refresh rule fired."""
        return bool(self.refresh)

    def text(self) -> str:
        """Plain-language monitoring report."""
        o = self.overall
        lines = [
            "WIN MODEL MONITORING",
            f"  Model trained on deals decided up to {self.trained_through}.",
        ]
        if not o.get("n"):
            lines.append("  No deals decided since then -- nothing to compare yet.")
            return "\n".join(lines)
        lines += [
            f"  Since then: {int(o['n']):,} decided quotes with a recorded price position.",
            f"  Predicted win rate {100 * o['predicted']:.1f}%, actual {100 * o['actual']:.1f}% "
            f"({'more than chance explains' if o['flagged'] else 'within what chance explains'}).",
            f"  Last {RECENT_MONTHS} months together: predicted {100 * o['recent_predicted']:.1f}%, "
            f"actual {100 * o['recent_actual']:.1f}% "
            f"({'more than chance explains' if o['recent_flagged'] else 'within what chance explains'}).",
            "",
            "  Month by month:",
        ]
        for _, r in self.monthly.iterrows():
            mark = "  <-- off" if r["flagged"] else ""
            lines.append(
                f"    {r['month']}  {int(r['n_quotes']):>4} quotes  predicted "
                f"{100 * r['predicted_win_rate']:5.1f}%  actual {100 * r['actual_win_rate']:5.1f}%{mark}"
            )
        lines += ["", "  Have the deals changed? (0 = identical, over 0.25 = major change)"]
        for _, r in self.stability.iterrows():
            lines.append(f"    {r['input']:<24} {r['psi']:.3f}  {r['verdict']}")
        lines.append("")
        if self.refresh:
            lines.append("  RETRAIN THE MODEL:")
            lines += [f"    - {reason}" for reason in self.refresh]
        else:
            lines.append("  No refresh needed.")
        return "\n".join(lines)


def _gap(n: int, predicted: float, actual: float, p: np.ndarray) -> tuple[float, bool]:
    """z-score of an actual-vs-predicted gap, and whether it is flagged."""
    variance = float((p * (1 - p)).sum()) / max(n, 1) ** 2
    z = (actual - predicted) / np.sqrt(variance) if variance > 0 else 0.0
    return float(z), bool(abs(z) > Z_FLAG)


def monitor(
    predict, training: pd.DataFrame, recent: pd.DataFrame, features: tuple[str, ...],
    trained_through: str,
) -> MonitoringReport:
    """Compare a model's predictions with what happened, and new deals with old."""
    if recent.empty:
        return MonitoringReport(pd.DataFrame(), pd.DataFrame(), {"n": 0}, trained_through)

    recent = recent.copy()
    recent["p"] = predict(recent)
    recent["month"] = pd.to_datetime(recent["decided_at"]).dt.to_period("M").astype(str)

    rows = []
    for month, g in recent.groupby("month"):
        p = g["p"].to_numpy()
        z, flagged = _gap(len(g), p.mean(), g["won"].mean(), p)
        rows.append(
            {
                "month": month,
                "n_quotes": len(g),
                "predicted_win_rate": round(float(p.mean()), 4),
                "actual_win_rate": round(float(g["won"].mean()), 4),
                "z": round(z, 2),
                "flagged": flagged and len(g) >= MIN_DEALS_PER_MONTH,
            }
        )
    monthly = pd.DataFrame(rows)

    p_all = recent["p"].to_numpy()
    z_all, flagged_all = _gap(len(recent), p_all.mean(), recent["won"].mean(), p_all)
    last = recent[recent["month"].isin(sorted(recent["month"].unique())[-RECENT_MONTHS:])]
    p_last = last["p"].to_numpy()
    z_last, flagged_last = _gap(len(last), p_last.mean(), last["won"].mean(), p_last)
    overall = {
        "n": float(len(recent)),
        "predicted": float(p_all.mean()),
        "actual": float(recent["won"].mean()),
        "z": z_all,
        "flagged": float(flagged_all),
        "recent_n": float(len(last)),
        "recent_predicted": float(p_last.mean()),
        "recent_actual": float(last["won"].mean()),
        "recent_z": z_last,
        "recent_flagged": float(flagged_last),
    }

    stability = pd.DataFrame(
        [{"input": f, "psi": round(psi(training[f], recent[f]), 4)} for f in features]
    )
    stability["verdict"] = np.select(
        [stability["psi"] > PSI_MAJOR, stability["psi"] > PSI_MODERATE],
        ["major change", "moderate change"],
        default="stable",
    )
    report = MonitoringReport(monthly, stability, overall, trained_through)
    report.refresh = refresh_reasons(report)
    return report


def refresh_reasons(report: MonitoringReport) -> tuple[str, ...]:
    """The refresh rule: why the model should be retrained, or nothing."""
    reasons = []
    o = report.overall
    if o.get("flagged"):
        reasons.append(
            f"over all {int(o['n'])} new quotes it predicted {100 * o['predicted']:.1f}% "
            f"wins but {100 * o['actual']:.1f}% were won"
        )
    if o.get("recent_flagged"):
        reasons.append(
            f"over the last {RECENT_MONTHS} months ({int(o['recent_n'])} quotes) it predicted "
            f"{100 * o['recent_predicted']:.1f}% wins but {100 * o['recent_actual']:.1f}% were won"
        )
    major = report.stability[report.stability["psi"] > PSI_MAJOR]
    if len(major):
        reasons.append("new deals differ majorly in: " + ", ".join(major["input"]))
    return tuple(reasons)
