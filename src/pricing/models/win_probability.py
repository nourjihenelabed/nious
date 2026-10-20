"""Win probability (D6): how likely a quote is to be won at a given price."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

#: Share of decisions, latest first, held back to test calibration.
HOLDOUT_SHARE = 0.20
#: Fewest decided offers with a known price position worth fitting on.
MIN_OFFERS = 200
#: Simulated test periods used to measure how large a calibration gap chance alone produces.
CALIBRATION_SIMULATIONS = 500
#: Bootstrap refits used to put a range on the price effect.
BOOTSTRAP_FITS = 100
RANDOM_STATE = 20260702
#: Bins in the reliability table.
CALIBRATION_BINS = 10
#: The segment column added by `attach_segments`, and the prefix of the
#: price-by-segment interaction columns built from it.
SEGMENT_COLUMN = "segment"
INTERACTION_PREFIX = "price_x_segment__"
UNSEGMENTED = "unsegmented"
SEGMENT_TIMING_NOTE = (
    "segments are fitted on the whole book, so a quote's segment can draw on orders "
    "placed after it; the held-out score is slightly optimistic for that feature"
)


def segment_code(label: str) -> str:
    """A segment label as a safe column-name fragment ("price-taking" -> "price_taking")."""
    return re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_") or UNSEGMENTED


@dataclass
class WinData:
    """Decided offers, ready to model, and an account of what was left out."""

    frame: pd.DataFrame
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    #: Plain-language reason -> number of offers or deals it removed.
    excluded: dict[str, int] = field(default_factory=dict)
    #: What `price_position` is measured against, in words.
    position_meaning: str = "price relative to the reference price"
    #: Interaction columns (price position x segment), rebuilt from the frame
    #: whenever the price position changes. Empty without segments.
    interactions: tuple[str, ...] = ()
    #: Segment code -> the human label it came from.
    segment_labels: dict[str, str] = field(default_factory=dict)

    @property
    def priced(self) -> pd.DataFrame:
        """Offers whose price position is known: the rows the model learns from."""
        return self.frame[self.frame["price_position"].notna()]


def attach_segments(
    data: WinData, segment_of: dict[str, str], interactions: bool = True
) -> WinData:
    """Add each offer's customer segment, and price x segment interactions."""
    frame = data.frame.copy()
    labels = frame["customer_id"].astype(str).map(
        {str(k): v for k, v in segment_of.items()}
    ).fillna(UNSEGMENTED)
    frame[SEGMENT_COLUMN] = labels.map(segment_code)
    codes = {segment_code(lbl): lbl for lbl in labels.unique()}

    columns: tuple[str, ...] = ()
    if interactions:
        priced = frame[frame["price_position"].notna()]
        # Unsegmented customers share the baseline slope: they are the ones with too little
        # history to have a habit, so a slope of their own would be fitted on a handful of
        # quotes.
        order = priced[SEGMENT_COLUMN].value_counts().drop(UNSEGMENTED, errors="ignore")
        columns = tuple(f"{INTERACTION_PREFIX}{code}" for code in sorted(order.index[1:]))
    frame = _with_interactions(frame, columns)
    return WinData(
        frame=frame,
        numeric=tuple(dict.fromkeys((*data.numeric, *columns))),
        categorical=tuple(dict.fromkeys((*data.categorical, SEGMENT_COLUMN))),
        excluded=dict(data.excluded),
        position_meaning=data.position_meaning,
        interactions=columns,
        segment_labels=codes,
    )


def _with_interactions(frame: pd.DataFrame, columns) -> pd.DataFrame:
    """(Re)build every price x segment column from the current price position."""
    columns = [c for c in columns if str(c).startswith(INTERACTION_PREFIX)]
    if not columns:
        return frame
    out = frame.copy()
    for col in columns:
        code = col[len(INTERACTION_PREFIX):]
        out[col] = out["price_position"] * (out[SEGMENT_COLUMN].astype(str) == code)
    return out


@dataclass
class ReferencePrices:
    """Per product, the prices a recommendation is built on, in one currency."""

    products: pd.DataFrame
    currency: str
    #: from_currency, to_currency, valid_from, valid_to, rate.
    fx: pd.DataFrame = field(default_factory=pd.DataFrame)
    #: How the reference price was obtained, in words.
    method: str = ""

    def product(self, product_id: str) -> pd.Series | None:
        """One product's row, or None."""
        rows = self.products[self.products["product_id"] == product_id]
        return None if rows.empty else rows.iloc[0]

    def to_reference_currency(self, amount: float, currency: str, on: pd.Timestamp) -> float:
        """Convert an amount into the reference currency at the rate in force on a date."""
        if currency == self.currency or pd.isna(amount):
            return amount
        if self.fx.empty:
            return float("nan")
        rates = self.fx[
            (self.fx["from_currency"] == currency)
            & (self.fx["to_currency"] == self.currency)
            & (self.fx["valid_from"] <= on)
            & (self.fx["valid_to"].isna() | (on < self.fx["valid_to"]))
        ]
        if rates.empty:
            return float("nan")
        return float(amount) * float(rates.sort_values("valid_from")["rate"].iloc[-1])


@dataclass
class WinModel:
    """A fitted win-probability model, with its held-out report card."""

    pipeline: object
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    metrics: dict[str, float]
    reliability: pd.DataFrame
    price_effect: dict[str, float]
    position_range: tuple[float, float]
    n_train: int
    n_test: int
    trained_through: str
    position_meaning: str
    excluded: dict[str, int]
    #: The model fitted on the earlier 80% only, kept for back-testing drift.
    holdout_pipeline: object | None = None
    holdout_cutoff: str = ""
    problems: tuple[str, ...] = ()
    #: Price x segment columns the model was fitted with (empty without segments).
    interactions: tuple[str, ...] = ()
    #: Segment code -> {"label", "n_quotes", "win_change_per_5pct", "win_change_low",
    #: "win_change_high"}: the price effect on that segment's quotes, with its range.
    segment_price_effects: dict[str, dict] = field(default_factory=dict)
    #: Segments whose price effect is not clearly negative.
    segment_warnings: dict[str, str] = field(default_factory=dict)

    # The attributes the rest of the engine reads off any acceptance model.
    mode: str = "logistic"
    is_win_rate: bool = True
    n_scopes: int = 1
    n_abstained: int = 0

    @property
    def usable(self) -> bool:
        """True when the model may drive price recommendations."""
        return not self.problems

    @property
    def notes(self) -> tuple[str, ...]:
        """What this model is, in a sentence or two."""
        notes = [
            f"Learned from {self.n_train + self.n_test:,} won and lost quotes whose "
            f"price position was recorded ({self.position_meaning}).",
            self.price_effect_sentence(),
        ]
        if self.interactions:
            notes.append(SEGMENT_TIMING_NOTE)
        notes += [f"Not used for prices: {p}" for p in self.problems]
        return tuple(notes)

    @property
    def uses_segments(self) -> bool:
        """True when the customer's segment is one of the model's inputs."""
        return SEGMENT_COLUMN in self.categorical

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        """Probability of winning for each row."""
        rows = _prepare(frame, self.categorical, self.numeric)
        return self.pipeline.predict_proba(rows[[*self.numeric, *self.categorical]])[:, 1]

    def curve(self, context: pd.DataFrame, positions: np.ndarray) -> np.ndarray:
        """Average win probability over a set of deal situations, at each price position."""
        keep = [c for c in (*self.numeric, *self.categorical) if c in context.columns]
        rows = context[keep].reset_index(drop=True)
        grid = rows.loc[rows.index.repeat(len(positions))].copy()
        grid["price_position"] = np.tile(positions, len(rows))
        p = self.predict(grid).reshape(len(rows), len(positions))
        return p.mean(axis=0)

    def calibration(self) -> pd.DataFrame:
        """Held-out reliability: predicted against observed win rate, by band."""
        return self.reliability

    def summary(self) -> pd.DataFrame:
        """The model's report card as one table."""
        rows = [{"measure": k, "value": v} for k, v in self.metrics.items()]
        rows += [{"measure": f"price_effect_{k}", "value": v} for k, v in self.price_effect.items()]
        rows += [
            {"measure": f"segment_price_effect_{code}_{k}", "value": v}
            for code, effect in self.segment_price_effects.items()
            for k, v in effect.items()
            if k != "label"
        ]
        return pd.DataFrame(rows)

    def price_effect_sentence(self) -> str:
        """The price effect, in words a salesperson can use."""
        e = self.price_effect
        return (
            f"Raising the price by 5% changes the chance of winning by "
            f"{e['win_change_per_5pct']:+.1f} percentage points on a typical deal "
            f"(likely between {e['win_change_low']:+.1f} and {e['win_change_high']:+.1f})."
        )

    def report(self) -> str:
        """Plain-language report card."""
        m = self.metrics
        lines = [
            "WIN PROBABILITY MODEL",
            f"  Trained on {self.n_train:,} decided quotes, tested on the next "
            f"{self.n_test:,} (decided after {self.holdout_cutoff}).",
            f"  Win rate in the test period: {100 * m['test_win_rate']:.1f}%.",
            f"  {self.price_effect_sentence()}",
            "",
            "  How good are the probabilities? (on the test deals)",
            f"    Predicted win rate {100 * m['predicted_win_rate']:.1f}%, actual "
            f"{100 * m['test_win_rate']:.1f}%",
            f"    Average gap between predicted and actual, band by band: "
            f"{100 * m['calibration_error']:.1f} points (chance alone explains up to "
            f"{100 * m['calibration_error_by_chance']:.1f} with this many deals)",
            f"    Ranks wins above losses {100 * m['auc']:.0f}% of the time "
            "(50% = guessing, 100% = perfect)",
            f"    Prediction error (Brier score): {m['brier']:.3f}, against "
            f"{m['brier_no_model']:.3f} for always guessing the average win rate",
            "",
        ]
        if self.segment_price_effects:
            lines.append("  Price effect by customer segment (+5% on price, likely range):")
            for code, e in self.segment_price_effects.items():
                flag = "  <- not clearly negative" if code in self.segment_warnings else ""
                lines.append(
                    f"    {e['label']:<40} {e['win_change_per_5pct']:+5.1f} points "
                    f"({e['win_change_low']:+.1f} to {e['win_change_high']:+.1f}), "
                    f"{int(e['n_quotes']):,} quotes{flag}"
                )
            if "log_loss_without_segment" in m:
                better = m["log_loss"] < m["log_loss_without_segment"]
                lines.append(
                    f"  With the segment the test log loss is {m['log_loss']:.4f}, without "
                    f"it {m['log_loss_without_segment']:.4f}: the segment "
                    f"{'helps' if better else 'does not help'} on later deals."
                )
            lines += [f"  Note: {SEGMENT_TIMING_NOTE}.", ""]

        if self.excluded:
            lines.append("  Left out of training:")
            lines += [f"    {n:>6,}  {reason}" for reason, n in self.excluded.items()]
            lines.append("")
        if self.problems:
            lines.append("  NOT USED FOR PRICE RECOMMENDATIONS:")
            lines += [f"    - {p}" for p in self.problems]
        else:
            lines.append("  Passed all checks: used for price recommendations.")
        return "\n".join(lines)


def _pipeline(numeric: tuple[str, ...], categorical: tuple[str, ...]):
    """Scaling and one-hot encoding in front of a logistic regression."""
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline, make_pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    prep = ColumnTransformer(
        [
            ("num", make_pipeline(SimpleImputer(strategy="median"), StandardScaler()),
             list(numeric)),
            ("cat", OneHotEncoder(handle_unknown="ignore"), list(categorical)),
        ]
    )
    return Pipeline([("prep", prep), ("model", LogisticRegression(max_iter=2000))])


def _prepare(
    frame: pd.DataFrame, categorical: tuple[str, ...], numeric: tuple[str, ...] = ()
) -> pd.DataFrame:
    """Categoricals as text, and price x segment columns rebuilt from the price."""
    out = frame.copy()
    for col in categorical:
        out[col] = out[col].astype(str)
    return _with_interactions(out, numeric)


def _bands(p: np.ndarray, bins: int) -> np.ndarray:
    """Band index per prediction, bands holding equal numbers of quotes."""
    order = np.argsort(p, kind="stable")
    band = np.empty(len(p), dtype=int)
    band[order] = np.arange(len(p)) * bins // max(len(p), 1)
    return band


def reliability_table(y: np.ndarray, p: np.ndarray, bins: int = CALIBRATION_BINS) -> pd.DataFrame:
    """Predicted against observed win rate, in bands of equal size."""
    band = _bands(p, bins)
    rows = []
    for b in range(bins):
        mask = band == b
        if not mask.any():
            continue
        rows.append(
            {
                "predicted_band": f"{100 * p[mask].min():.0f}-{100 * p[mask].max():.0f}%",
                "n_quotes": int(mask.sum()),
                "predicted_win_rate": round(float(p[mask].mean()), 4),
                "actual_win_rate": round(float(y[mask].mean()), 4),
            }
        )
    return pd.DataFrame(rows)


def calibration_error(y: np.ndarray, p: np.ndarray, bins: int = CALIBRATION_BINS) -> float:
    """Expected calibration error: the quote-weighted gap between predicted and actual."""
    if len(p) == 0:
        return float("nan")
    band = _bands(p, bins)
    gap = 0.0
    for b in np.unique(band):
        mask = band == b
        gap += mask.sum() * abs(p[mask].mean() - y[mask].mean())
    return float(gap / len(p))


def chance_calibration_error(p: np.ndarray, bins: int = CALIBRATION_BINS) -> float:
    """The gap a perfectly calibrated model would exceed only 5% of the time."""
    rng = np.random.default_rng(RANDOM_STATE)
    gaps = [
        calibration_error((rng.random(len(p)) < p).astype(int), p, bins)
        for _ in range(CALIBRATION_SIMULATIONS)
    ]
    return float(np.percentile(gaps, 95))


def evaluate(pipeline, frame: pd.DataFrame, numeric, categorical) -> dict[str, float]:
    """Held-out quality of a fitted pipeline on decided offers."""
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    y = frame["won"].to_numpy(dtype=int)
    p = pipeline.predict_proba(frame[[*numeric, *categorical]])[:, 1]
    base = np.full_like(p, y.mean(), dtype=float)
    both = len(np.unique(y)) == 2
    return {
        "n": float(len(y)),
        "test_win_rate": float(y.mean()),
        "predicted_win_rate": float(p.mean()),
        "brier": float(brier_score_loss(y, p)),
        "brier_no_model": float(brier_score_loss(y, base)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "auc": float(roc_auc_score(y, p)) if both else float("nan"),
        "calibration_error": calibration_error(y, p),
        "calibration_error_by_chance": chance_calibration_error(p),
    }


def _price_effect(pipeline, frame: pd.DataFrame, numeric, categorical) -> float:
    """Average change in win probability, in points, when every quote is 5% dearer."""
    rows = frame[[*numeric, *categorical]]
    base = pipeline.predict_proba(rows)[:, 1]
    dearer = rows.copy()
    dearer["price_position"] = dearer["price_position"] + 0.05
    dearer = _with_interactions(dearer, numeric)
    return float(100 * (pipeline.predict_proba(dearer)[:, 1] - base).mean())


def fit(data: WinData, holdout_share: float = HOLDOUT_SHARE) -> WinModel:
    """Fit, test on later deals, then refit on everything for use."""
    numeric = tuple(dict.fromkeys(("price_position", *data.numeric)))
    categorical = tuple(data.categorical)
    priced = _prepare(data.priced.sort_values("decided_at"), categorical, numeric)

    if len(priced) < MIN_OFFERS or priced["won"].nunique() < 2:
        raise ValueError(
            f"only {len(priced)} decided quotes have a recorded price position "
            f"(at least {MIN_OFFERS}, with both wins and losses, are needed)"
        )

    cut = int(round(len(priced) * (1 - holdout_share)))
    train, test = priced.iloc[:cut], priced.iloc[cut:]
    holdout = _pipeline(numeric, categorical).fit(train[[*numeric, *categorical]], train["won"])
    metrics = evaluate(holdout, test, numeric, categorical)
    reliability = reliability_table(
        test["won"].to_numpy(dtype=int),
        holdout.predict_proba(test[[*numeric, *categorical]])[:, 1],
    )

    final = _pipeline(numeric, categorical).fit(priced[[*numeric, *categorical]], priced["won"])

    # A range on the price effect, from refits on resampled deals. Resampling
    # deals rather than adding noise keeps every refit a plausible history.
    segmented = SEGMENT_COLUMN in categorical
    groups = (
        {code: priced[priced[SEGMENT_COLUMN] == code] for code in sorted(priced[SEGMENT_COLUMN].unique())}
        if segmented
        else {}
    )
    rng = np.random.default_rng(RANDOM_STATE)
    effects = []
    by_segment: dict[str, list[float]] = {code: [] for code in groups}
    for _ in range(BOOTSTRAP_FITS):
        sample = priced.iloc[rng.integers(0, len(priced), len(priced))]
        if sample["won"].nunique() < 2:
            continue
        refit = _pipeline(numeric, categorical).fit(
            sample[[*numeric, *categorical]], sample["won"]
        )
        effects.append(_price_effect(refit, priced, numeric, categorical))
        for code, rows in groups.items():
            by_segment[code].append(_price_effect(refit, rows, numeric, categorical))
    central = _price_effect(final, priced, numeric, categorical)
    low, high = (np.percentile(effects, [5, 95]) if effects else (np.nan, np.nan))

    segment_effects: dict[str, dict] = {}
    segment_warnings: dict[str, str] = {}
    for code, rows in groups.items():
        draws = by_segment[code]
        s_low, s_high = np.percentile(draws, [5, 95]) if draws else (np.nan, np.nan)
        label = data.segment_labels.get(code, code)
        segment_effects[code] = {
            "label": label,
            "n_quotes": float(len(rows)),
            "win_change_per_5pct": _price_effect(final, rows, numeric, categorical),
            "win_change_low": float(s_low),
            "win_change_high": float(s_high),
        }
        if not s_high < 0:
            segment_warnings[code] = (
                f"in the {label} segment the data does not show that higher prices lose "
                f"more deals (effect of +5%: between {s_low:+.1f} and {s_high:+.1f} points)"
            )

    if segmented:
        # Does knowing the segment help on later deals? The same split, without it.
        plain_numeric = tuple(c for c in numeric if c not in data.interactions)
        plain_categorical = tuple(c for c in categorical if c != SEGMENT_COLUMN)
        plain = _pipeline(plain_numeric, plain_categorical).fit(
            train[[*plain_numeric, *plain_categorical]], train["won"]
        )
        from sklearn.metrics import log_loss as _log_loss

        p_plain = plain.predict_proba(test[[*plain_numeric, *plain_categorical]])[:, 1]
        metrics["log_loss_without_segment"] = float(
            _log_loss(test["won"].to_numpy(dtype=int), p_plain, labels=[0, 1])
        )

    problems = []
    if not high < 0:
        problems.append(
            "the data does not show that higher prices lose more deals "
            f"(effect of +5%: between {low:+.1f} and {high:+.1f} points), so the "
            "model cannot say where the best price is"
        )
    if metrics["calibration_error"] > metrics["calibration_error_by_chance"]:
        problems.append(
            f"on the test deals its probabilities are off by "
            f"{100 * metrics['calibration_error']:.1f} points on average, more than the "
            f"{100 * metrics['calibration_error_by_chance']:.1f} that chance alone explains"
        )
    if not metrics["brier"] < metrics["brier_no_model"]:
        problems.append("on the test deals it predicts no better than the average win rate")

    positions = priced["price_position"]
    return WinModel(
        pipeline=final,
        numeric=numeric,
        categorical=categorical,
        metrics=metrics,
        reliability=reliability,
        price_effect={
            "win_change_per_5pct": central,
            "win_change_low": float(low),
            "win_change_high": float(high),
        },
        position_range=(float(positions.min()), float(positions.max())),
        n_train=len(train),
        n_test=len(test),
        trained_through=str(pd.Timestamp(priced["decided_at"].max()).date()),
        position_meaning=data.position_meaning,
        excluded=dict(data.excluded),
        holdout_pipeline=holdout,
        holdout_cutoff=str(pd.Timestamp(train["decided_at"].max()).date()),
        problems=tuple(problems),
        interactions=tuple(data.interactions),
        segment_price_effects=segment_effects,
        segment_warnings=segment_warnings,
    )
