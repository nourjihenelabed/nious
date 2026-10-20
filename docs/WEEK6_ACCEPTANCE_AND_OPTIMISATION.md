# Week 6 (D6) — acceptance probability and constrained price optimisation

Owner: Nourjihen Elabed · engine v0.4.0 · 178 tests

The plan calls this "win probability + optimiser". The first half of that phrase
turned out not to be available on this data, and most of the week went into
working out what to do about it that was not either pretending or giving up.

---

## 1. There are no lost quotes

A win-probability model is logistic regression on won and lost quotes. This graph
has 700 transactions and every one of them completed. There are no losses. No
amount of statistical sophistication recovers a win rate from a dataset with one
class in it.

Three things I could have done:

1. **Fit anyway on the won deals.** Returns a model that predicts "won" with 100%
   accuracy and 100% confidence, and is worthless. This is the version that looks
   finished in a demo.
2. **Return nothing.** Honest, and leaves the optimiser with no price-response
   function, which means no D6.
3. **Estimate a different quantity and label it precisely.** What I did.

### The quantity that *is* estimable

Given the discount depth a proposed price implies, what share of comparable
historical deals closed at a depth no greater than it?

That is the empirical distribution of accepted discounts — a **concession
benchmark**. If 75% of comparable deals closed at a depth of 12% or less, then
offering 12% is at least as attractive to the customer as three quarters of what
this market has historically accepted.

It is not a win rate. It is bounded in [0, 1], monotone in price, and will be
read as a win rate by anyone not told otherwise, which is exactly why the module
is built around not letting that happen:

- `mode="empirical_acceptance"` and `is_win_rate=False` on the model
- `interpretation` on **every** prediction, carried into `Confidence.reasons`
- confidence capped below HIGH on any recommendation built from it, however many
  comparable deals stand behind it
- `calibration()` refuses to score:

```
calibration requires observed outcomes; this source records only completed
transactions, so there is nothing to be right or wrong about
```

The logistic path is written into the interface and raises `NotImplementedError`
with a pointer, so the day a source arrives with lost quotes the shape is already
there.

### Why calibration, not accuracy

Accuracy asks whether we predicted the right outcome. Calibration asks whether
"seventy percent" actually means seventy percent. The optimiser multiplies
expected margin straight through this number, so a model that ranks prices
correctly but is numerically overconfident produces confidently wrong prices —
and they look exactly like confidently right ones.

That is also why refusing to compute a calibration score is the honest move
rather than a gap. Scoring the benchmark against won deals alone returns a
perfect 100% that means nothing, and a perfect score in a report is worse than a
blank, because nobody interrogates it.

### Smoothing

The estimate is the Laplace-smoothed empirical CDF, `(k + 1) / (n + 2)`. It never
reaches 0 or 1. A price nobody has ever been offered should not come back as
certainly accepted, and a price deeper than every historical discount should not
come back as certainly rejected.

---

## 2. The optimiser

Expected profit at price `p`:

```
E[profit](p) = P(accept | depth(p)) x (p - unit_cost)
```

Raising price raises the margin and lowers acceptance, so the maximum sits
somewhere in between. That is the whole model. Everything else exists to stop it
being trusted further than it deserves.

### Constraints come first, not last

The grid is built **inside** the feasible region:

| constraint | why |
|---|---|
| at least cost / (1 − margin floor) | never recommend a price below the 5% margin floor |
| at most list price | list is the ceiling a negotiation starts from |
| within ±25% of the comparable price | an optimum nobody has ever been charged is a hypothesis, not a recommendation |

An optimiser that finds an unconstrained optimum and then clips it reports a
number nobody may act on. Every constraint that actually bound is listed in
`constraints_applied` on the contract record, so Track B can say *why* the
recommendation is where it is.

### A band, not a point

`PriceRange` is the region where expected profit stays within 2% of its maximum.
The curve near its peak is usually flat, so a single price implies a precision the
evidence cannot support — and a band is what a negotiator can actually use.
`contracts.PriceRange` rejects an unordered band at construction.

### What it says

Over the 60 most-traded customer-product pairs:

```
customer_id product_id      low      mid     high  current  uplift%  acceptance  n
    CUS-033    PRD-011   647.78   660.40   665.82   614.14    +7.53        0.75  55
    CUS-009    PRD-006  1699.10  1703.84  1703.84  1608.76    +5.91        0.65  47
    CUS-006    PRD-009  1544.03  1563.58  1573.36  1747.72   -10.54        0.92  58
```

8 of 60 pairs are priced below the recommended band, with a median uplift of
5.4%. The negative rows are as useful as the positive ones: CUS-006 is paying
above what the acceptance curve says is optimal, which is a retention risk rather
than an opportunity.

### It abstains more often than it answers

No comparable deals, no cost, no list price, or a feasible region that comes out
empty — each produces `ABSTAINED` with the reason attached, not a number. On a
real book most customer-product pairs have traded twice and will abstain. On this
synthetic snapshot the 60 most-traded pairs all clear the threshold, which is a
property of synthetic data, not a property of the method.

---

## 3. Elasticity is deliberately not in the loop

Week 5 fitted elasticities and none of the 21 products passed
`fit_for_optimisation`. The optimiser therefore does not use them. It could have:
an elasticity gives a demand curve and a revenue-maximising price directly. But
wiring an ungated elasticity into a price recommendation is how a module ends up
recommending a 20% cut on the strength of a slope whose credible interval crosses
zero.

The acceptance benchmark is a weaker instrument and a more defensible one: it is
a statement about what this market has already agreed to, not a claim about what
demand will do next.

---

## 4. What Track B receives

```python
run = pipeline.run(with_models=True)
run.result.recommendations   # PriceRecommendation, banded, evidenced, constrained
```

Every record carries its price band, its acceptance probability with the
benchmark caveat attached, the count of comparable deals, the constraints that
bound it, and the customer and product ids it was computed from.
`contracts.validate()` passes: no COMPLETE record without a price range, no
recommendation with zero comparables that is not abstained.

---

## 5. Honest limits, carried into Week 7

1. **Acceptance is a proxy.** Until a source records lost quotes, this is a
   concession benchmark. The single highest-value ask of the commercial team is a
   CRM export with lost opportunities in it.
2. **Elasticity is unidentified.** Observational prices, no experiment. A
   structured price test on a handful of SKUs would change this more than any
   modelling.
3. **Cost is average, not marginal.** `unit_cost` is a standard cost. The margin
   floor is therefore approximate, and on a make-to-order line it could be
   materially wrong.
4. **No competitor price anywhere in the graph.** Acceptance is modelled purely
   from own history, which cannot see a competitor moving.

---

## 6. Progress against the plan

| Week | Deliverable | Status |
|---|---|---|
| 1 | Environment, graph profiling, data write-up, quick win | done |
| 2 | Graph-access layer **D1**, module structure, result schema **D8 draft** | done |
| 3 | Price Waterfall Engine **D2** + hand-computed reference | done |
| 4 | Margin analytics + Pricing Data Readiness Score **D3** | done |
| 5 | Segmentation **D4**, elasticity **D5** | done |
| 6 | Acceptance + constrained optimiser **D6** | done |
| 7–8 | MLOps packaging **D7**, freeze the contract, integrate | next |

Week 7 is packaging: freeze `CONTRACT_VERSION` at 0.2.0, register the models in
MLflow with the readiness score as a gate, and put `run_readiness.py` in front of
the DVC pipeline so a snapshot that cannot support a deliverable stops the run
instead of producing one.
