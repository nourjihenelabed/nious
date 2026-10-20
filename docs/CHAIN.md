# The model chain: how each stage feeds the next

Owner: Nourjihen Elabed · engine v0.4.0 · contract v0.2.0-draft · October 2026

The onboarding plan and the presentation describe the engine as a **chain**:

```
graph -> read & check -> price waterfall -> readiness gate -> segmentation
      -> elasticity -> win probability -> optimiser -> result contract -> Track B
```

Until this change, the first half was a chain and the second half was not.
Segmentation read the waterfall but nothing read segmentation. Elasticity was
fitted per product and ignored the segments. The win model used one price effect
for every customer. The optimiser never looked at elasticity. This document
describes the three links that were added, the check that guards the first of
them, how the number of segments is now chosen, and what each did on the v2 data.

Numbers below are from `dvc repro` on `data/snapshot_axiom_v2` (MLflow run
`87c65e33cec34497b1c54f7147c985bb`).

---

## 0. Choosing the number of segments (D4, the open item)

**Before:** k was fixed at 4, "the number a commercial team can act on".

**Now:** `segmentation.choose_k` tries every k from 2 to 8 with **two methods**,
k-means and hierarchical (Ward) clustering, and scores each fit with:

- **Silhouette** (−1 to 1): how much closer a customer is to its own group than to
  the next one.
- **Elbow**: the k after which one more group stops explaining much. It is read
  as a number: for each k, the drop in within-group error from k−1 to k divided by
  the drop from k to k+1; the largest ratio is the elbow.

The rule: take the best k-means silhouette; any k within 0.02 of it is a tie;
among the ties prefer the elbow k, otherwise the smallest. Hierarchical labels
replace k-means only when they separate clearly better (by more than 0.02),
because k-means centres can score a new customer later and a dendrogram cannot.

| v2 result | |
|---|---|
| chosen | **k = 3, k-means**, silhouette 0.302 |
| tied | k = 3, 4, 5; elbow at 3 |
| k-means vs hierarchical | agree on 85% of the grouping (adjusted Rand 0.85) |
| reading | weak but real structure: treat labels as tendencies |

The full table goes to `model_segment_k_selection.csv` and to MLflow.

A first version used the "kneedle" elbow (furthest point from a straight line).
A planted five-group test showed it picks a group too early when one early drop
is very large, so it was replaced. That test stays in `tests/test_chain.py`.

---

## 1. Segmentation → elasticity

**Before:** one elasticity per product (`elasticity_scope = "product"`).

**Now:** one elasticity **per customer segment** (`elasticity.fit_by_segment`,
the new default `elasticity_scope = "segment"`).

How it is measured, in plain terms:

1. A point is one product, bought by one segment, in one month: its recorded price
   and the volume that moved.
2. Each product's own average price and volume are removed inside each segment.
   What is left is "this product's volume moved when its price moved", so a
   segment that buys cheap bulk products and dear specialities is not mistaken for
   a price-sensitive one.
3. One slope per segment, then **shrinkage**: each segment is pulled toward the
   book average (all segments pooled), more strongly the less its own data says.
4. Every estimate keeps its **credible interval** and the **fit-for-optimisation
   gate** (enough points, enough price movement, interval entirely below zero).

| segment | elasticity | 94% interval | fit to drive a price |
|---|---|---|---|
| negotiated but profitable | −2.46 | −2.72 to −2.20 | yes |
| volume-driven, heavily negotiated | −2.36 | −2.63 to −2.09 | yes |
| price-taking | −2.32 | −2.58 to −2.06 | yes |
| book average | −2.39 | | |

About 60% of each estimate comes from the book average. For a borrowed prior
that would be a weakness and the gate would fail it. Here the prior is the book's
own slope, fitted on these same points, so heavy pooling says "the segments
barely differ from each other", which is a finding. It is reported, not failed.
That is the same rule the PyMC backend already followed.

### The circularity check

Five of the eight segmentation features are price behaviour: realisation,
leakage, off-invoice share, margin, realisation spread. Grouping customers by how
they pay and then measuring how they respond to price risks finding what was put
in. So `elasticity.circularity_check`:

1. refits the segments **without any price feature** (breadth, cadence, size only),
   at the same k;
2. measures elasticity inside those price-blind segments;
3. compares how much the segments differ from each other (spread of their slopes)
   in the two cases.

If the price-built segments differ more than **2×** as much as the price-blind
ones, part of the difference is suspected to come from the segmentation, and **no
segment elasticity is used for optimisation**. The verdict is written into every
estimate's reasons.

| dataset | spread, price-built | spread, price-blind | ratio | verdict |
|---|---|---|---|---|
| v2 graph | 0.15 | 0.09 | 1.5 | passed |
| synthetic snapshot | 1.56 | 0.38 | 4.1 | **failed**: segment elasticities not used |

The synthetic result is the check doing its job: it is why it exists.

---

## 2. Segmentation → win probability

**Before:** logistic regression on won/lost quotes with one price effect for
every customer.

**Now:** `win_probability.attach_segments` adds

- the customer's **segment** as a category, and
- one **price × segment** column per segment except the most common one (the
  baseline), so the price effect can differ by segment. Customers without a
  segment share the baseline slope rather than get one fitted on a few quotes.

The interaction columns are rebuilt from the price every time the model is
asked a question, so moving the price on the optimiser's ladder moves the
interaction with it.

It still has to pass the **three checks** before it may set a price, and does:

| check | v2 result |
|---|---|
| higher prices lose more deals | −10.7 points per +5% (likely −11.8 to −9.9) ✓ |
| probabilities right on later deals | off by 6.8 points; chance alone explains up to 8.3 ✓ |
| better than guessing the average win rate | Brier 0.223 vs 0.238 ✓ |

And knowing the segment **helps on later deals**: test log loss 0.632 with it,
0.642 without.

| segment | change in win chance for +5% on price | 90% range |
|---|---|---|
| negotiated but profitable | −11.5 points | −13.1 to −10.0 |
| volume-driven, heavily negotiated | −10.9 points | −15.0 to −6.8 |
| price-taking | −9.8 points | −11.9 to −8.0 |

A segment whose range is not entirely below zero would be named in the report,
and the optimiser lowers confidence for its customers. None is, on v2.

**Honest caveat, also in the model report:** segments are fitted on the whole
book, so a quote's segment can draw on orders placed after it. A buying habit
changes slowly, but the held-out score is slightly optimistic for that feature.

---

## 3. Elasticity → optimiser (the volume rule)

**Before:** floor = cost + 5%, ceiling = list price, and a minimum chance of
winning. Elasticity was not used.

**Now:** a fourth rule. For each candidate price the optimiser estimates the
volume the customer would keep:

```
volume kept at price p  =  (p / today's price) ^ elasticity
```

using the customer's **segment** elasticity at the **most elastic end of its
interval**, so uncertainty makes the rule stricter, never looser. Prices that keep
less than **80%** of today's volume (`min_volume_share`) are removed before the
best price is picked.

- Only elasticities that passed the fit-for-optimisation gate are used.
- If **none** passed, the rule is skipped for every pair, and that is written in
  the recommendation's `constraints_applied` and in its confidence reasons;
  confidence is multiplied by 0.9.
- A customer with no segment gets no volume rule, with the reason stated.
- Every priced recommendation states the volume expected at its mid price, with a
  range from the interval: "at the mid price, volume is expected at 87% of today's
  (between 86% and 89%)".

| v2 result | |
|---|---|
| pairs priced | 172 of 200 |
| bands limited by the volume rule | 37 |
| pairs refused because no tested price keeps 80% of volume | 17 (today's price is 10–28% below the cheapest price ever tested for them) |
| pairs paying less today than recommended | 113, median increase 6.7% |

---

## 4. Two links outside the models that were also missing

- **The contract Track B reads had no models in it.** `engine_result.json` was
  written only by the waterfall stage, so its `segments`, `elasticities` and
  `recommendations` were empty. `run_models.py` now writes it with everything, and
  in `dvc.yaml` the models stage owns that file and runs after the waterfall.
- **The contract did not name its MLflow run.** `provenance.mlflow_run_id` was
  always null. It is now set to the run that recorded the result, so a contract
  leads back to its parameters, metrics and registered model.

Every link's status for a run is written to `model_links.json` and tagged on the
MLflow run (`link__…`).

---

## How to check it

```bash
pytest tests/test_chain.py -m "slow or not slow"   # 27 tests: every link, planted data + v2
python scripts/run_models.py --profile axiom_v2 --graph data/snapshot_axiom_v2 --out reports_axiom_v2
python -m dvc repro                                  # everything, tracked in MLflow
python demo/server.py                                # the demo, http://127.0.0.1:8765
```

## What is still open

- **Docker image not built.** The Dockerfile is there; Docker is not installed on
  the development machine.
- **The PyMC backend cannot run here.** The C++ compiler at `C:\gcc` is broken
  (`cc1plus` missing), so the engine uses the conjugate backend and the five MCMC
  tests in `tests/test_hierarchical.py` fail on this machine. They do not depend on
  this change.
- **Promotion is a decision, not a default.** The segment-aware win model is
  registered as version 6, *candidate*, with a better test log loss than the
  current champion (0.632 vs 0.642). Promote with
  `python scripts/manage_models.py promote --profile axiom_v2` once reviewed.
- **Monitoring flags drift.** Over the last three back-test months the model
  predicted 33% wins where 44% were won, more than chance explains. That is a
  refresh signal, reported by `monitor_axiom_v2`.
