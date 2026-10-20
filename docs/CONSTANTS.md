# Where every number in this module comes from

Owner: Nourjihen Elabed · engine v0.4.0

A threshold that nobody can source is a threshold nobody can defend, and one that
gets quietly lowered the first time it blocks a demo. This is the register: every
hardcoded value the engine uses, what it is, and what it rests on.

Three kinds of provenance, and they are not equally good:

| | meaning |
|---|---|
| **Published** | a citation with a page you can hand to a reviewer |
| **Convention** | a community default with a traceable origin, but no derivation for this use |
| **Judgement** | somebody chose it. Defensible, arguable, and **not** science |

The rule this document exists to enforce: **a number without a source is a client
decision, not an engine decision.** It belongs in `pyproject.toml` where the
client can see and set it — not in a constant where it reads as a finding.

---

## 1. Published

### MCMC convergence gates — `models/hierarchical.py`

```python
MAX_RHAT = 1.01
MIN_ESS = 400
DEFAULT_CHAINS = 4
DEFAULT_DRAWS = 1_000
DEFAULT_TUNE = 1_000
```

Vehtari, Gelman, Simpson, Carpenter & Bürkner (2021), *Rank-normalization,
folding, and localization: An improved R̂ for assessing convergence of MCMC*,
**Bayesian Analysis** 16(2), 667–718.

The paper's stated default guideline is rank-normalised split-R̂ below **1.01**
and bulk-ESS above **400**, *for four chains of 1,000 post-warmup iterations*.
All five constants come from that one sentence, and they are a package: the 400
is only the right number because there are four chains of a thousand. Change the
chain count and the ESS floor is no longer the paper's.

This is the cleanest provenance in the module. An elasticity whose chains fail
either test is barred from optimisation, and that bar is Vehtari's, not ours.

### Laplace smoothing — `models/acceptance.py`

```python
SMOOTHING = 1.0
```

Laplace's rule of succession (*Essai philosophique sur les probabilités*, 1814):
add one success and one failure to an empirical frequency so that an event never
observed is not reported as impossible, and one always observed is not reported
as certain. Add-one is the classical form.

Here it stops an acceptance curve returning 0% or 100% for a depth at the edge of
the observed range, which the optimiser would otherwise multiply straight through
to an infinite or zero expected margin.

### The waterfall itself — `spec.py`, `profiles.py`

The chain of levels and deductions from list price to pocket margin, and the
claim that the off-invoice band is the larger and less-managed drain, are from
Marn & Rosiello (1992), *Managing Price, Gaining Profit*, **Harvard Business
Review**, Sept–Oct 1992. The 15–20%-of-list leakage figure quoted in the
presentation is theirs. No threshold depends on it; it is the method's source.

---

## 2. Convention

### Credible interval mass — `models/elasticity.py`

```python
CREDIBLE_MASS = 0.94
```

The ArviZ default HDI probability, itself following McElreath (*Statistical
Rethinking*) in choosing a value near but deliberately **not** 95%, so that an
interval excluding zero does not get read as a significance test at α = 0.05. A
posterior interval is not a hypothesis rejection, and the odd number is there to
keep the reflex from firing.

Traceable and defensible. Not derived — any value in that region would serve.

### Sampler step-size target — `models/hierarchical.py`

```python
TARGET_ACCEPT = 0.9
```

Stan's and PyMC's default is 0.8; 0.9 is the standard first response to
divergent transitions in a hierarchical model, recommended in the Stan reference
manual's divergence guidance. Raising it trades speed for smaller steps.

Convention with a reason, not a result.

### Small-cell suppression — `config.py`

```python
MIN_TRANSACTIONS = 3
```

A group of fewer than three documents is not reported as a number. This matches
the *k*-anonymity floor used in statistical disclosure control, where k = 3 to 5
is the common range for published cells. It is doing two jobs at once here —
disclosure and noise suppression — and only the first has a literature.

### Money comparison tolerance — `config.py`

```python
TOLERANCE = 0.01
```

One cent. Currency, not statistics.

---

## 3. Judgement — and therefore client decisions

**Every number in this section was chosen, not derived.** Where a literature
exists, it is named, and in two cases it says the module is asking for *less*
evidence than it should.

### Depth thresholds — `config.RECOMMENDED_DEPTH`

```python
{"waterfall": 1, "margin_analytics": 20, "segmentation": 60,
 "elasticity": 120, "optimisation": 200}
```

| threshold | status |
|---|---|
| `waterfall = 1` | **Sound.** The waterfall is arithmetic. One document is a valid waterfall. |
| `margin_analytics = 20` | **Judgement.** Medians and quartiles on fewer than ~20 points are unstable, but 20 is a round number, not a result. |
| `elasticity = 120` | **Supported, and the closest call.** Green (1991), *How Many Subjects Does It Take To Do A Regression Analysis?*, **Multivariate Behavioral Research** 26(3), 499–510, finds N ≥ 104 + m for testing an individual predictor. A log-log elasticity has m = 1, so **N ≥ 105**. 120 clears it; 100 would not have. |
| `segmentation = 60` | **Contradicted by the literature — too low.** See below. |
| `optimisation = 200` | **Judgement.** Chosen as "more than elasticity, because it compounds two estimates." There is no derivation. |

### Segmentation depth — the one to raise, not lower

```python
SEGMENT_MIN_DEALS = 5
SEGMENT_CLUSTERS = 4
DEPTH_REQUIREMENTS["segmentation"] = 60
```

Dolnicar, Grün, Leisch & Schmidt (2014), *Required Sample Sizes for Data-Driven
Market Segmentation Analyses in Tourism*, **Journal of Travel Research** 53(3),
296–306, run simulations on artificial data of known structure and conclude that
a sample of **70 times the number of segmentation variables** is adequate under
all conditions tested.

`segmentation.FEATURES` holds **8** variables. So the literature asks for
**70 × 8 = 560 customers**.

The AXIOM PoC graph has **41**. The synthetic snapshot has ~50. Neither is
within an order of magnitude of what the published requirement is, and the
module's own threshold of 60 *documents* is not the same quantity as 560
*customers* — it is both a different unit and a far weaker bar.

`SEGMENT_MIN_DEALS = 5` has no source at all. `SEGMENT_CLUSTERS = 4` used to fix
the number of clusters as well, which was "the number a commercial team can act
on" — a statement about meetings. **That is fixed:** k is now chosen from the data
by `segmentation.choose_k` (silhouette plus elbow, k-means compared with
hierarchical clustering; see "The model chain" below). `SEGMENT_CLUSTERS` now only
says how many qualifying customers readiness asks for before segmentation runs.

The v2 graph has **560 segmentable customers — exactly 70 × 8**, the first
dataset in this project to meet the Dolnicar et al. bar.

**Consequence, stated plainly:** segmentation on this data is a proposal for
discussion, not a finding, whatever the threshold says. Lowering `MIN_DEALS` to
make it run does not change that; it only removes the warning.

### Per-scope model minimums

```python
elasticity.MIN_OBSERVATIONS = 8
elasticity.TRUSTED_OBSERVATIONS = 20
elasticity.MIN_LOG_PRICE_SD = 0.02
acceptance.MIN_COMPARABLES = 8
```

All judgement. `MIN_OBSERVATIONS = 8` is well below Green's N ≥ 105 for the same
regression — it is not a claim that eight points identify a slope, but the point
below which shrinkage toward the pooled prior stops being able to rescue the
estimate at all. `TRUSTED_OBSERVATIONS = 20` marks where the module stops
treating a fitted slope as usable for pricing; it is a caution line, not a test.

`MIN_LOG_PRICE_SD = 0.02` is the only one with a mechanism behind it: with no
variation in price there is no slope to identify, at any sample size. The
mechanism is sound; the value 0.02 is not derived from anything.

### Optimiser policy

```python
MARGIN_FLOOR = 0.05
REFERENCE_BAND = 0.25
TOLERANCE_OF_MAX = 0.02
GRID_POINTS = 121
```

`MARGIN_FLOOR` and `REFERENCE_BAND` are **commercial policy wearing a
statistician's coat.** Whether a price must clear cost by 5% and may sit 25%
away from what comparable deals paid are questions for whoever owns pricing at
the client. They are constants here only because nobody has been asked yet.
They belong in `pyproject.toml` with the client's name on them.

`TOLERANCE_OF_MAX` and `GRID_POINTS` are numerical resolution — how flat a peak
counts as flat, how finely the grid is searched. Harmless, arbitrary, and with
no effect on any published number beyond the width of a band.

### The model chain — `models/segmentation.py`, `models/elasticity.py`, `models/optimiser.py`

Added when the stages were linked (see [`CHAIN.md`](CHAIN.md)).

| constant | value | status |
|---|---|---|
| `config.SEGMENT_K_RANGE` | 2 to 8 | **Judgement.** Fewer than 2 is not a segmentation; more than 8 is more groups than a pricing team will price differently. |
| `segmentation.SILHOUETTE_TIE` | 0.02 | **Judgement.** Silhouette moves by a few hundredths between seeds at this size, so a smaller gap is not evidence for one k over another. |
| `segmentation.SILHOUETTE_WEAK` / `_REASONABLE` | 0.25 / 0.50 | **Convention.** Kaufman & Rousseeuw (1990), *Finding Groups in Data*: 0.26–0.50 weak structure, 0.51–0.70 reasonable, above 0.70 strong. Used for the wording only, never as a gate. |
| `elasticity.CIRCULARITY_RATIO` | 2.0 | **Judgement.** How much more price-built segments may differ in price sensitivity than price-blind ones before the difference is suspected of coming from the segmentation. |
| `elasticity.MIN_SPREAD` | 0.10 | **Judgement.** A floor on the price-blind spread, so near-zero noise cannot turn into a huge ratio. |
| `config.MIN_VOLUME_SHARE` | 0.80 | **Judgement, and a client decision.** The volume rule: a price is removed if the segment elasticity (at the most elastic end of its interval) expects it to keep less than 80% of today's volume. In `pyproject.toml` as `min_volume_share`. |
| `optimiser.NO_VOLUME_RULE_FACTOR` | 0.9 | **Judgement.** Confidence multiplier when the volume rule could not run for a pair: one guard was not checked. |

### Confidence bands

```python
CONFIDENCE_HIGH = 0.80
CONFIDENCE_MEDIUM = 0.50
THIN_EVIDENCE_DEALS = 10
PROXY_CONFIDENCE_CAP = 0.79
```

Judgement throughout. `PROXY_CONFIDENCE_CAP` is the most defensible of them, and
not because of its value: it is set just below `CONFIDENCE_HIGH` so that a
recommendation built on a concession benchmark can never be labelled high
confidence, however many comparable deals stand behind it. The *mechanism* is the
point; 0.79 is just "less than 0.80".

---

## What to do about section 3

Do not lower a judgement threshold to make a model run. It was not precise enough
to be worth defeating, and defeating it destroys the only signal the module has
that the answer is thin.

Two honest moves when a deliverable abstains:

1. **Let it abstain, and report why.** `run_readiness.py` names the missing
   evidence, and the result contract carries the abstention with its reason all
   the way to Track B. An abstention is an output.
2. **Change what the estimate is about.** Fitting elasticity at family grain
   rather than product grain is not a loosened threshold — it is a different and
   smaller claim, made on evidence that supports it. Set
   `PRICING_ELASTICITY_SCOPE=family`; the result says which grain it was fitted
   at, and `config.depth_reservations()` records the choice.

Anything set below `config.RECOMMENDED_DEPTH` is listed by `run_readiness.py`
under "Thresholds lowered for this run" and attached to every model fitted under
it. That machinery exists so a deviation can be argued about, not so it can be
routine.

---

## Sources

- Vehtari, Gelman, Simpson, Carpenter & Bürkner (2021), *Rank-normalization, folding, and localization: An improved R̂ for assessing convergence of MCMC*, Bayesian Analysis 16(2), 667–718. <https://avehtari.github.io/rhat_ess/rhat_ess.html>
- Green (1991), *How Many Subjects Does It Take To Do A Regression Analysis?*, Multivariate Behavioral Research 26(3), 499–510. <https://eric.ed.gov/?id=EJ436937>
- Dolnicar, Grün, Leisch & Schmidt (2014), *Required Sample Sizes for Data-Driven Market Segmentation Analyses in Tourism*, Journal of Travel Research 53(3), 296–306. <https://journals.sagepub.com/doi/full/10.1177/0047287513496475>
- Marn & Rosiello (1992), *Managing Price, Gaining Profit*, Harvard Business Review, Sept–Oct 1992.
- Laplace (1814), *Essai philosophique sur les probabilités* — the rule of succession.
- Gelman & Rubin (1992), *Inference from Iterative Simulation Using Multiple Sequences*, Statistical Science 7(4), 457–472 — the original R̂, superseded for our purposes by Vehtari et al.
