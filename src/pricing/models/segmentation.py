"""Behavioural segmentation (D4, Week 5): segments earned from behaviour, not from labels."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import config
from ..contracts import schema as C
from ..engine.aggregation import aggregate
from ..engine.waterfall import Waterfall, ratio

#: Minimum certified deals before a customer is worth labelling at all.
MIN_DEALS = config.SEGMENT_MIN_DEALS
#: Fewest qualifying customers readiness asks for before segmentation runs. It
#: used to be the number of clusters as well; k is now chosen by `choose_k`.
DEFAULT_K = config.SEGMENT_CLUSTERS
SEED = 20260824
#: Silhouette scores within this distance of the best count as a tie.
SILHOUETTE_TIE = 0.02
#: Silhouette readings.
SILHOUETTE_WEAK = 0.25
SILHOUETTE_REASONABLE = 0.50

#: Feature order is fixed so the standardisation and the centroids stay comparable
#: between runs. Each is a rate or a count, never a raw amount, except `log_size`.
FEATURES: tuple[str, ...] = (
    "price_realisation_pct",
    "total_leakage_pct",
    "off_invoice_intensity",
    "pocket_margin_pct",
    "realisation_dispersion",
    "product_breadth",
    "deal_cadence",
    "log_size",
)


@dataclass
class Segmentation:
    """A fitted segmentation: the assignment, the features, and the fit itself."""

    assignments: pd.DataFrame
    features: pd.DataFrame
    centroids: pd.DataFrame
    k: int
    n_abstained: int = 0
    reasons: dict[str, str] = field(default_factory=dict)
    #: One row per (method, k) tried: inertia, silhouette, and which was chosen.
    selection: pd.DataFrame = field(default_factory=pd.DataFrame)
    #: How the final k and method were chosen, in words.
    selection_note: str = ""
    method: str = "kmeans"
    silhouette: float | None = None
    #: Agreement between k-means and hierarchical labels at the chosen k
    #: (adjusted Rand index: 1 = identical groups, 0 = no better than chance).
    method_agreement: float | None = None
    features_used: tuple[str, ...] = ()

    def label_of(self) -> dict[str, str]:
        """Customer -> segment label, for the customers that were labelled."""
        done = self.assignments[self.assignments["status"] == "complete"]
        return dict(zip(done["customer_id"], done["segment_label"], strict=True))

    def profile_table(self) -> pd.DataFrame:
        """One row per segment: size, and the feature means that named it."""
        labelled = self.assignments[self.assignments["status"] == "complete"]
        extra = [
            c for c in self.features.columns
            if c == "customer_id" or c not in labelled.columns
        ]
        joined = labelled.merge(self.features[extra], on="customer_id", how="left")
        out = joined.groupby("segment_label").agg(
            n_customers=("customer_id", "nunique"),
            n_deals=("n_deals", "sum"),
            pocket_price=("pocket_price", "sum"),
            **{f: (f, "mean") for f in FEATURES if f in joined.columns},
        )
        out["share_of_value_pct"] = (
            100 * out["pocket_price"] / out["pocket_price"].sum()
        ).round(2)
        return out.reset_index().sort_values("pocket_price", ascending=False, ignore_index=True)

    def to_contract(self, w: Waterfall) -> tuple[C.SegmentAssignment, ...]:
        """The assignment as Track B receives it, evidence and confidence attached."""
        currencies = _currency_by_customer(w)
        records = []
        for _, row in self.assignments.iterrows():
            complete = row["status"] == "complete"
            currency = currencies.get(row["customer_id"], "EUR")
            reasons = _confidence_reasons(row, complete)
            records.append(
                C.SegmentAssignment(
                    customer_id=row["customer_id"],
                    segment_label=row["segment_label"],
                    segment_score=_finite(row.get("segment_score")),
                    price_sensitivity_index=_finite(row.get("price_sensitivity_index")),
                    volume_tier=row.get("volume_tier"),
                    margin_contribution=(
                        C.Money(round(float(row["pocket_margin"]), 2), currency)
                        if pd.notna(row.get("pocket_margin")) and currency != "MIXED"
                        else C.Money.unavailable(
                            "this customer trades in more than one currency and the "
                            "graph carries no FX rate"
                            if currency == "MIXED"
                            else "margin not decomposable",
                            currency,
                        )
                    ),
                    status=C.ResultStatus.COMPLETE if complete else C.ResultStatus.ABSTAINED,
                    confidence=C.Confidence.from_score(row["confidence"], reasons),
                    evidence=C.Evidence(customer_ids=(row["customer_id"],)),
                    distinguishing_features=tuple(
                        str(x) for x in (row.get("distinguishing_features") or ())
                    ),
                )
            )
        return tuple(records)


def _finite(value) -> float | None:
    """A float, or None where the number does not exist."""
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def _currency_by_customer(w: Waterfall) -> dict[str, str]:
    """Each customer's currency, or MIXED where they trade in more than one."""
    col = w.profile.currency_column
    if not col or col not in w.keys.columns or "customer_id" not in w.keys.columns:
        return {}
    grouped = w.keys.groupby("customer_id")[col]
    return {
        customer: (codes[0] if len(codes) == 1 else "MIXED")
        for customer, codes in grouped.agg(lambda s: sorted(s.dropna().unique())).items()
    }


def _confidence_reasons(row: pd.Series, complete: bool) -> tuple[str, ...]:
    """Why a segment assignment is less than certain."""
    if not complete:
        return (row["note"],)
    reasons = []
    if pd.notna(row.get("segment_score")) and row["segment_score"] < 0.5:
        reasons.append(
            "this customer sits near the boundary between two behaviours, so the "
            "label is the closer of two similar fits"
        )
    if int(row.get("n_deals", 0)) < 3 * MIN_DEALS:
        reasons.append(f"{int(row['n_deals'])} certified deals is a short behavioural history")
    return tuple(reasons)


def build_features(w: Waterfall) -> pd.DataFrame:
    """One row per customer: how they buy, expressed as rates rather than amounts."""
    profile = w.profile
    if profile.dimension("customer") is None:
        raise ValueError(
            f"profile {profile.name!r} has no customer dimension, so there is nobody to segment"
        )

    by_customer = aggregate(w, "customer").set_index("customer_id")
    keys = w.keys[w.keys["certified"]]
    facts = w.facts[w.facts["certified"]]
    price = _price_level(profile)

    per_deal = keys.assign(
        _realisation=ratio(keys[price], keys[profile.opening])
    ).groupby("customer_id")

    f = pd.DataFrame(index=by_customer.index)
    f["n_deals"] = by_customer["n_transactions_certified"]
    f["pocket_price"] = by_customer[price]
    f["pocket_margin"] = by_customer[profile.closing]
    f["price_realisation_pct"] = by_customer["price_realisation_pct"]
    f["total_leakage_pct"] = by_customer["total_leakage_pct"]
    f["pocket_margin_pct"] = by_customer["pocket_margin_pct"]

    # Which *kind* of concession a customer extracts, not just how much.
    leakage = by_customer[profile.opening] - by_customer[price]
    off_invoice = by_customer.get("off_invoice_leakage")
    f["off_invoice_intensity"] = (
        ratio(off_invoice, leakage) if off_invoice is not None else np.nan
    )

    f["realisation_dispersion"] = per_deal["_realisation"].std()
    f["product_breadth"] = facts.groupby("customer_id")["product_id"].nunique()

    date_col = profile.date_column
    if date_col and date_col in keys.columns:
        span = keys.groupby("customer_id")[date_col].agg(["min", "max"])
        months = ((span["max"] - span["min"]).dt.days / 30.44).clip(lower=1.0)
        f["deal_cadence"] = f["n_deals"] / months
    else:
        f["deal_cadence"] = np.nan

    f["log_size"] = np.log1p(f["pocket_price"].clip(lower=0))
    return f.reset_index()


def _standardise(values: np.ndarray) -> np.ndarray:
    """Zero mean, unit variance per column, with constant columns left at zero."""
    mean = np.nanmean(values, axis=0)
    std = np.nanstd(values, axis=0)
    std = np.where(std == 0, 1.0, std)
    out = (values - mean) / std
    return np.nan_to_num(out, nan=0.0)


def _name_clusters(centroids: pd.DataFrame) -> dict[int, str]:
    """Name each cluster from where its centroid sits, not from its index."""
    margin = centroids["pocket_margin_pct"]
    leakage = centroids["total_leakage_pct"]
    size = centroids["log_size"]

    names = {}
    for idx in centroids.index:
        good_margin = margin[idx] >= margin.median()
        heavy = leakage[idx] >= leakage.median()
        large = size[idx] >= size.median()
        if good_margin and not heavy:
            label = "premium anchor" if large else "price-taking"
        elif good_margin and heavy:
            label = "negotiated but profitable"
        elif heavy:
            label = (
                "volume-driven, heavily negotiated" if large
                else "small and heavily discounted"
            )
        else:
            # Thin margin without heavy negotiation is the interesting one: the
            # price was never the problem, so the answer is cost to serve or mix.
            label = "thin margin, little discounting -- check cost to serve"
        names[idx] = label

    # Two clusters can land on the same description; keep them distinguishable
    # rather than silently merging what the fit says are different behaviours.
    seen: dict[str, int] = {}
    for idx, label in names.items():
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            names[idx] = f"{label} ({seen[label]})"
    return names


def _elbow(ks: list[int], inertia: list[float]) -> int:
    """The k after which one more group buys far less than the last one did."""
    if len(ks) < 3:
        return ks[0]
    y = np.asarray(inertia, dtype=float)
    before = y[:-2] - y[1:-1]
    after = np.maximum(y[1:-1] - y[2:], 1e-12)
    return ks[1 + int(np.argmax(before / after))]


def _inertia(matrix: np.ndarray, labels: np.ndarray) -> float:
    """Within-group sum of squares, so hierarchical fits sit on the same elbow axis."""
    return float(
        sum(
            ((matrix[labels == c] - matrix[labels == c].mean(axis=0)) ** 2).sum()
            for c in np.unique(labels)
        )
    )


def choose_k(
    matrix: np.ndarray, k_range: tuple[int, int] = config.SEGMENT_K_RANGE, seed: int = SEED
) -> tuple[int, str, pd.DataFrame, str]:
    """Pick the number of segments and the clustering method from the data."""
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.metrics import silhouette_score

    n = len(matrix)
    low, high = k_range
    ks = list(range(max(2, low), min(high, n - 1) + 1))
    if not ks:
        raise ValueError(f"{n} customers cannot be split into two or more segments")

    rows = []
    for k in ks:
        km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(matrix)
        ward = AgglomerativeClustering(n_clusters=k, linkage="ward").fit(matrix)
        for method, labels, inertia in (
            ("kmeans", km.labels_, float(km.inertia_)),
            ("hierarchical", ward.labels_, _inertia(matrix, ward.labels_)),
        ):
            rows.append(
                {
                    "method": method,
                    "k": k,
                    "inertia": round(inertia, 3),
                    "silhouette": round(float(silhouette_score(matrix, labels)), 4),
                    "smallest_segment": int(np.bincount(labels).min()),
                }
            )
    table = pd.DataFrame(rows)
    kmeans = table[table["method"] == "kmeans"].set_index("k")
    elbow = _elbow(ks, list(kmeans["inertia"]))
    best = float(kmeans["silhouette"].max())
    tied = sorted(k for k in ks if kmeans.loc[k, "silhouette"] >= best - SILHOUETTE_TIE)
    k = elbow if elbow in tied else tied[0]

    ward_score = float(
        table[(table["method"] == "hierarchical") & (table["k"] == k)]["silhouette"].iloc[0]
    )
    km_score = float(kmeans.loc[k, "silhouette"])
    method = "hierarchical" if ward_score > km_score + SILHOUETTE_TIE else "kmeans"

    table["elbow"] = table["k"] == elbow
    table["chosen"] = (table["k"] == k) & (table["method"] == method)
    note = (
        f"k={k} by {method}: best k-means silhouette {best:.3f}; k within "
        f"{SILHOUETTE_TIE} of it: {', '.join(map(str, tied))}; elbow of the "
        f"within-group error at k={elbow}. At k={k} k-means scores {km_score:.3f}, "
        f"hierarchical {ward_score:.3f}."
    )
    return k, method, table, note


def structure_reading(silhouette: float | None) -> str:
    """The silhouette in words (Kaufman & Rousseeuw's reading)."""
    if silhouette is None:
        return "not measured"
    if silhouette > SILHOUETTE_REASONABLE:
        return "reasonable structure"
    if silhouette > SILHOUETTE_WEAK:
        return "weak structure: the groups are real but overlap, so read labels as tendencies"
    return "no substantial structure: the groups overlap more than they separate"


def fit(
    w: Waterfall,
    k: int | None = None,
    min_deals: int = MIN_DEALS,
    seed: int = SEED,
    features_used: tuple[str, ...] = FEATURES,
) -> Segmentation:
    """Cluster customers on how they buy, abstaining where the evidence is thin."""
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.metrics import adjusted_rand_score, silhouette_score

    features = build_features(w)
    eligible = features[features["n_deals"] >= min_deals].copy()
    thin = features[features["n_deals"] < min_deals].copy()

    if k is None and config.SEGMENT_K_SELECTION != "auto":
        k = int(config.SEGMENT_K_SELECTION)
    needed = k if k is not None else 3
    if len(eligible) < needed:
        raise ValueError(
            f"{len(eligible)} customer(s) have at least {min_deals} certified deals, "
            f"which cannot support {needed} clusters. Lower k, lower min_deals, or "
            "accept that this book is not segmentable."
        )

    matrix = _standardise(eligible[list(features_used)].to_numpy(dtype=float))
    if k is None:
        k, method, selection, selection_note = choose_k(matrix, seed=seed)
    else:
        method, selection = "kmeans", pd.DataFrame()
        selection_note = f"k={k} fixed by the caller or by settings, not chosen from the data"

    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(matrix)
    labels = km.labels_
    agreement = None
    if 1 < k < len(matrix):
        ward = AgglomerativeClustering(n_clusters=k, linkage="ward").fit(matrix)
        agreement = round(float(adjusted_rand_score(km.labels_, ward.labels_)), 4)
        if method == "hierarchical":
            labels = ward.labels_
    eligible["cluster"] = labels
    silhouette = (
        round(float(silhouette_score(matrix, labels)), 4)
        if 1 < len(np.unique(labels)) < len(matrix)
        else None
    )

    centroids = eligible.groupby("cluster")[list(FEATURES)].mean()
    names = _name_clusters(centroids)
    centroids.insert(0, "segment_label", [names[i] for i in centroids.index])

    eligible["segment_label"] = eligible["cluster"].map(names)
    eligible["status"] = "complete"
    eligible["note"] = ""

    # Distance to the assigned centroid, mapped to a 0-1 fit score: a customer on
    # the boundary between two behaviours should not be described as certainly one.
    centres = np.vstack([matrix[labels == c].mean(axis=0) for c in range(k)])
    distance = np.linalg.norm(matrix - centres[labels], axis=1)
    eligible["segment_score"] = np.round(1.0 / (1.0 + distance), 4)

    # Price sensitivity: how much they negotiate away, blended with how much that
    # varies deal to deal. Both are inputs to whether a discount will be demanded.
    leakage_z = _standardise(eligible[["total_leakage_pct"]].to_numpy(dtype=float))[:, 0]
    dispersion_z = _standardise(
        eligible[["realisation_dispersion"]].to_numpy(dtype=float)
    )[:, 0]
    eligible["price_sensitivity_index"] = np.round(
        1 / (1 + np.exp(-(0.7 * leakage_z + 0.3 * dispersion_z))), 4
    )

    tiers = pd.qcut(
        eligible["pocket_price"].rank(method="first"), 3, labels=["small", "mid", "large"]
    )
    eligible["volume_tier"] = tiers.astype(str)
    eligible["confidence"] = np.clip(
        eligible["segment_score"] * np.minimum(1.0, eligible["n_deals"] / (3 * min_deals)),
        0.0,
        1.0,
    )
    eligible["distinguishing_features"] = _distinguishing(eligible, centroids)

    if len(thin):
        thin["segment_label"] = "unsegmented"
        thin["status"] = "abstained"
        thin["note"] = (
            f"fewer than {min_deals} certified deals -- not enough behaviour to read"
        )
        thin["segment_score"] = np.nan
        thin["price_sensitivity_index"] = np.nan
        thin["volume_tier"] = None
        thin["confidence"] = 0.0
        thin["distinguishing_features"] = [() for _ in range(len(thin))]

    assignments = pd.concat([eligible, thin], ignore_index=True)
    cols = [
        "customer_id", "segment_label", "segment_score", "price_sensitivity_index",
        "volume_tier", "n_deals", "pocket_price", "pocket_margin", "confidence",
        "status", "note", "distinguishing_features",
    ]
    return Segmentation(
        assignments=assignments[cols],
        features=features,
        centroids=centroids.reset_index(),
        k=k,
        n_abstained=len(thin),
        selection=selection,
        selection_note=selection_note,
        method=method,
        silhouette=silhouette,
        method_agreement=agreement,
        features_used=tuple(features_used),
    )


def _distinguishing(eligible: pd.DataFrame, centroids: pd.DataFrame) -> list[tuple[str, ...]]:
    """The two features on which each customer's cluster is most unlike the rest."""
    overall = centroids[list(FEATURES)].mean()
    spread = centroids[list(FEATURES)].std().replace(0, np.nan)
    deviation = ((centroids[list(FEATURES)] - overall) / spread).abs()

    top: dict[int, tuple[str, ...]] = {}
    for cluster in centroids.index:
        ranked = deviation.loc[cluster].sort_values(ascending=False)
        top[cluster] = tuple(
            f"{name} {'high' if centroids.loc[cluster, name] > overall[name] else 'low'}"
            for name in ranked.index[:2]
        )
    return [top[c] for c in eligible["cluster"]]


def _price_level(profile) -> str:
    """The level margin is measured against."""
    from ..engine.waterfall import _price_level as level

    return level(profile)
