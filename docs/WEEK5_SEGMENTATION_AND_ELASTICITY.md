# Week 5 (D4, D5) — behavioural segmentation and price elasticity

Owner: Nourjihen Elabed · engine v0.4.0 · 178 tests

Both deliverables run against `b2b_industrial`. The dairy mapping cannot carry
either — no customer, no date — and the readiness score says so before anything
is fitted rather than after something crashes.

---

## 1. Segmentation (D4)

### The segment the graph already has is the wrong one

`customers.segment` holds Enterprise / Mid-Market / SMB. That is a *descriptive*
segmentation: it says how big a customer is. Pricing needs a different thing —
how they buy. Two Mid-Market accounts of identical revenue can behave completely
differently: one takes list and reorders, the other extracts an off-invoice
rebate every quarter. Priced the same way, one of them is being overserved.

So the features are relative, not absolute:

| feature | why |
|---|---|
| `price_realisation_pct` | pocket over list: what they actually pay |
| `total_leakage_pct` | how much is given away between list and pocket |
| `off_invoice_intensity` | *which kind* of concession — rebate-after-the-fact behaves differently from an invoice discount |
| `pocket_margin_pct` | what the relationship earns |
| `realisation_dispersion` | how consistently they are priced deal to deal |
| `product_breadth` | how much of the catalogue they buy |
| `deal_cadence` | orders per active month |
| `log_size` | one absolute feature, so size is not entirely discarded |

Only one of the eight is absolute. Include more and k-means rediscovers the
revenue ranking and calls it insight.

### What came out

k-means, k=4, fixed seed, on standardised features. 40 customers, none abstained
at the default 5-deal threshold.

| segment | customers | pocket price | share | margin % | leakage % |
|---|---|---|---|---|---|
| volume-driven, heavily negotiated | 10 | 10.34M | 45.7% | 7.0% | 35.4% |
| thin margin, little discounting — check cost to serve | 15 | 9.28M | 41.0% | 26.5% | 19.2% |
| negotiated but profitable | 6 | 1.81M | 8.0% | 26.9% | 20.9% |
| price-taking | 9 | 1.21M | 5.4% | 32.6% | 12.3% |

The first row is the finding: ten customers, 46% of the book, giving away 35% of
list and earning 7%.

### Three properties that matter more than the algorithm

**Labels are economic, not ordinal.** `cluster_2` is not a finding. Each cluster
is named from where its centroid sits on margin and negotiation intensity, so the
name survives a re-fit that permutes cluster indices. The fourth quadrant —
thin margin *without* heavy discounting — is the one I nearly left out, and it is
the most actionable: the price was never the problem, so the answer is cost to
serve or mix.

**It abstains.** Clustering assigns everyone to a cluster; that is the failure
mode, not the feature. A customer below `min_deals` gets `status=ABSTAINED`,
`segment_label="unsegmented"` and no score. A confident label on two deals is
indistinguishable from a confident label on two hundred, and Track B has no way
to tell them apart unless the engine does.

**Every record explains itself.** `distinguishing_features` names the two
features on which the customer's cluster deviates most from the book, and
`Confidence.reasons` carries boundary proximity and short history. The contract
rejects a low-confidence record with no reason, which is how I found that the
first version was shipping bare LOW bands.

---

## 2. Elasticity (D5)

### The bug I shipped first

The first version derived price as `value / quantity` and fitted log-log per
product. Elasticities came out around **-10**, which for industrial B2B is
absurd. The cause is arithmetic, not economics: if `p = v / q`, then
`log p = log v - log q`, so quantity sits on both sides of the regression and
drags the slope negative before any demand behaviour is involved. Division bias.

The fix is to use a price the source *recorded* — `transaction_lines.unit_price`
— and take its median per scope-period. The profile now declares
`unit_price_column`, and where a source has no recorded price the fallback is
used, `price_source` says so, and every affected estimate is barred from
optimisation.

Refitted on the recorded price, the pooled slope is **-6.5 ± 5.3**. Still steep,
still wide — this is synthetic data where price and quantity were generated
independently, so there is no true elasticity to find, and a module that
confidently found one would be broken.

### Shrinkage: two estimators, one interface

A product with a handful of observations will happily produce -7.4 and mean
nothing by it. Both estimators pull each scope toward the population; they differ
in how honestly they admit they do not know how strongly to pull.

**`backend="conjugate"`** — per-scope OLS, then the normal-normal conjugate
posterior, precision-weighted:

```
weight    = tau^2 / (tau^2 + se^2)          # 1 = keep own slope, 0 = take the prior
posterior = weight * own + (1 - weight) * prior
sd        = sqrt(1 / (1/tau^2 + 1/se^2))
```

Closed form, milliseconds, deterministic. Its weakness is that it is a *two-stage*
procedure: `tau` is estimated from the same slopes it then shrinks and is
thereafter treated as known, so the intervals come out a little too narrow.

**`backend="pymc"`** — one model over every scope, with the pooling strength
inferred rather than plugged in:

```
log q[i] = alpha[scope[i]] + beta[scope[i]] * log p[i] + noise
beta[s]  ~ Normal(mu_beta, sigma_beta)
mu_beta ~ Normal(-1, 2);  sigma_beta ~ HalfNormal(1)
```

Non-centred (`beta = mu + sigma * z`), because with few observations per scope the
centred form produces a funnel NUTS cannot traverse and you get divergences
instead of an answer. Log price is centred within scope, so `alpha` means "log
volume at this scope's own average price" rather than at a price of one.

**The two disagree, and the disagreement is the finding.** The conjugate backend
reports per-product slopes from -3.7 to -15.4 around a pooled -6.5 with a spread
of 5.3. The hierarchical model estimates `sigma_beta = 0.72` — the products barely
differ — and returns everything near -5.7 with 93–98% pooling. It concludes the
apparent per-product variation *is* noise, which is exactly the question the
conjugate form assumed the answer to.

Because that is inference rather than assumption, heavy pooling is reported under
PyMC and gated under the conjugate backend. There, high shrinkage means "your own
data was too thin to trust"; here it means the model measured how much scopes
differ and found they hardly do, and the interval already carries that
uncertainty.

**Diagnostics are gates.** R-hat, effective sample size and divergence count come
back on every estimate, and a scope whose chains did not mix is barred from
optimisation exactly like one whose interval crosses zero. On this snapshot the
fit is clean: 0 divergences, max R-hat 1.01, min ESS 1181.

**Choosing.** `backend="auto"` (the default) prefers PyMC when it is installed
*and* can run quickly, and states its reason when it does not. On this machine
PyTensor finds a `g++` whose `cc1plus` is missing — `g++ --version` prints
happily and every real compile fails — so a fit that should take seconds runs on
the NumPy backend instead. `auto` therefore picks the conjugate form and says so
rather than appearing to hang. `backend="pymc"` still works; the posterior is
identical.

**A bug worth recording, because it cost five hours.** The switch to the NumPy
backend has to happen *before* PyTensor is imported — it reads the setting at
import time, and importing PyMC imports PyTensor. My first version set
`pytensor.config.cxx = ""` one line too late, after an `available()` check that
imports PyMC. The flag then had no effect, so every graph in the model spawned a
compiler that failed in turn: the same fit took **615 seconds** with the flag set
up front and **20,554 seconds** with it set afterwards. Same posterior, 33x the
time, and nothing in the output said why.

The probe now runs through `subprocess` and deliberately does not import
PyTensor, because probing through PyTensor is what closes the window it is
probing for. Verified after the fix on an isolated 3-scope, 200-draw fit: 73
seconds, no compiler invoked, `PYTENSOR_FLAGS='cxx='` set before the import —
roughly 0.37s per draw, which is the honest cost of the NumPy backend rather than
the cost of failing to compile several hundred times.

That per-draw cost is also the argument for `auto` preferring the conjugate form
here. It is not that sampling is wrong on this machine; it is that sampling is
about three orders of magnitude slower than the closed form, and a default nobody
can wait for is a default nobody uses.

**Scale.** The model runs on *aggregated* observations — one row per scope per
period — so its size is set by how many scopes and periods exist, not by how many
transactions were aggregated. 21 products x 18 months is 356 rows. A thousand
SKUs over three years is 36,000 and still fits. The raw transaction count is a
pandas problem, not a PyMC one.

### A gate, not a caveat

`fit_for_optimisation` is False unless the scope clears all of:

- at least 20 observations
- a credible interval that stays entirely negative
- a recorded price rather than a derived one
- under the conjugate backend, less than half the estimate borrowed from the prior
- under PyMC, chains that converged: R-hat <= 1.01, ESS >= 400, no divergences

**0 of 21 products pass on this snapshot.** That is the correct answer, and it is
why the optimiser in Week 6 does not depend on elasticity at all. A caveat in a
footnote gets dropped by the second person to read it; a boolean the optimiser
checks does not.

### The one thing that cannot be fixed here

Prices are observational. Nobody randomised them. If discounts were granted
*because* a large order was coming, the fitted slope carries that reverse
causality and overstates how much volume a price cut buys. Every estimate carries
`identification` saying so, and it travels into `Confidence.reasons` on the
contract record. It is also why the gate is on price variation rather than on
R-squared: a tight fit to endogenous prices is a better-looking wrong answer, not
a better one.

Fixing it properly needs a price experiment, which is a Week 7+ conversation with
the commercial team, not a modelling choice.

---

## 3. What Track B receives

Both deliverables fold into the same `EngineResult`:

```python
run = pipeline.run(with_models=True)
run.result.segments         # 40 SegmentAssignment
run.result.elasticities     # 21 ElasticityEstimate
```

`contracts.validate()` passes on the full payload. The two rules it enforces here
are the ones I would otherwise have broken: no point estimate without a credible
interval, and no low-confidence record without a stated reason.

---

## 4. Into Week 6

Win probability and the constrained optimiser (D6). The interesting problem is
already visible: this graph records only completed transactions, so there are no
losses to fit a win rate against. Write-up in
[`WEEK6_ACCEPTANCE_AND_OPTIMISATION.md`](WEEK6_ACCEPTANCE_AND_OPTIMISATION.md).
