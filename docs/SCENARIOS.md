# Three scenarios, one engine

Owner: Nourjihen Elabed · engine v0.4.0

The same knowledge graph in three states, run through the same engine. The code,
the profile and the backend do not change between them — only the data does. That
is the claim, and this is how to check it.

```bash
python scripts/scenarios.py
```

Press **b** the first time to build the datasets, then pick a scenario.
[`COMMANDS.md`](COMMANDS.md) has every command this runs underneath, plus MLflow,
DVC and the tests.

---

## The three

| | 1. as it arrived | 2. reduced | 3. topped up |
|---|---|---|---|
| orders | 100 | 12 | 1,361 |
| order lines | 262 | 38 | 3,462 |
| products bought | 78 | 31 | 80 |
| customers | 41 | 12 | 50 |
| **median lines per product** | **3** | **1** | **43** |
| currencies | 2 | 1 | 2 |
| coverage | 100% | 42% | 100% |
| readiness | 78.8 LIMITED | 72.0 LIMITED | 86.3 READY |
| waterfall | READY | LIMITED | READY |
| margin analytics | LIMITED | BLOCKED | LIMITED |
| segmentation | BLOCKED | BLOCKED | READY |
| elasticity | BLOCKED | BLOCKED | READY |
| optimisation | BLOCKED | BLOCKED | LIMITED |

The row in bold is the one that explains the table. Elasticity is fitted per
product and will not fit a scope with fewer than eight observations, so *median
lines per product* is the difference between BLOCKED and READY — and it is not
something engineering can supply.

---

## 1 — Data as it arrived

The delivered export, untouched.

**What it shows.** The waterfall is fully supported: every rung computable on
every one of the 100 orders, sliced by customer, channel, region, period and
segment, every slice reconciling with the grand total. Margin analytics runs with
a stated limit. Segmentation, elasticity and optimisation are refused, each with
the reason and the missing quantity named.

**What to look at.** The four validation warnings, which are all real statements
about the data:

- 80,032 in volume rebates posted against a **customer-quarter**, which spans
  orders that are not all in this book. Real leakage, reported beside the
  waterfall rather than allocated into it.
- 90 deductions marked as accruals — an estimate of money that will be paid, not
  a record of money that was.
- 400 of the 500 opportunities produced no order.
- Two currencies, and no exchange rate anywhere in the graph.

**The currency caveat.** The printed headline of 6,677,812.50 adds 6.5m Saudi
riyal to 160k euro. The typed contract the downstream team receives is emitted per
currency and marks every mixed group `ABSTAINED`, so no mixed number reaches a
consumer — but the convenience CSV and the console summary carry an unlabelled
grand total. The publishable split:

```bash
python - <<'EOF'
import sys; sys.path.insert(0, "src")
from pricing.engine import pipeline, aggregation as agg
run = pipeline.run(with_models=False, track=False)
by = agg.aggregate(run.waterfall, "currency")
print(by[["currencies", "n_transactions_certified", "list_value", "pocket_margin"]].to_string(index=False))
print("headline total:", run.waterfall.totals["list_value"])
EOF
```

---

## 2 — Data reduced

12 of the delivered orders, with a quarter of the product cost records removed.

Two different kinds of damage, on purpose, because the engine reports them
differently:

**The book got shorter.** 12 documents, so margin analytics — which wants 20 —
falls from LIMITED to BLOCKED.

**The book got holes.** Without a cost record, COGS cannot be computed on the
lines using those products. `waterfall_availability.csv` shows every rung at 100%
until `cogs` drops to 76% of lines and `pocket_margin` to 42%, and the flags table
names the cause with its blast radius: *no cost period covering the order date —
9 lines, 7 transactions.*

Coverage falls to **5 of 12**. Seven orders are excluded from the headline and
counted, rather than half-computed. That is the behaviour worth pointing at: a
conventional pipeline given the same data would have treated the missing cost as
zero, reported a 100% pocket margin on those seven, and produced a healthy-looking
headline that was wrong.

To move one lever at a time:

```bash
python scripts/make_scenario.py reduced --orders 12 --drop-cost-periods 0    # shorter, but whole
python scripts/make_scenario.py reduced --orders 40 --drop-cost-periods 0.5  # longer, but holed
```

---

## 3 — Data topped up until it passes

The delivered export **kept whole** — all 100 real orders still in the book — plus
added trade in the same catalogue, at the same real list prices and real costs, to
the same real customers, until every gate opens.

It answers one question: *what would have to be true of this data for the pipeline
to run end to end?* The generator prints the answer:

```
  delivered orders kept whole :    100
  orders added                :  1,261
  book now                    :  1,361
```

**It took 1,261 added orders, not twenty.** That number is the finding. The
delivered book is 100 orders over 78 products — about three lines each — and
elasticity is fitted per product, so what was missing was never a handful of
documents. It was depth in every product scope, and a price that moves for reasons
unconnected to the order.

Two things are deliberately built into the added trade that the delivered orders
do not have:

- **Depth per product.** Elasticity aggregates to product × month, so depth means
  a product appearing across many months, not simply more rows.
- **A price drawn before the demand that follows it.** Each product gets a price
  level per month drawn at random. In the delivered data every discount is 5%,
  10% or 15%, and twelve products only ever sold at a single price.

**What it does not fix.** Scenario 3 still spans two currencies, because trading
more does not supply an exchange rate. Volume problems are fixable by volume;
that one is not — it needs an FX rate in the graph.

`margin_analytics` also stays LIMITED on every dataset, and always will: freight
is posted once per order, so every step below the invoice price is known at order
grain and cannot be attributed to a product. That is a property of how concessions
are recorded, not of how much data there is.

### The acceptance test

The added orders were generated with a true price elasticity per product family
that the engine is never told. Recovering it is the check that the elasticity
module works:

```bash
export PRICING_PROFILE=axiom_poc
export PRICING_GRAPH_JSON=data/scenarios/axiom_topup_1361.json

python - <<'EOF'
import sys, json; sys.path.insert(0, "src")
from pricing.engine import pipeline
from pricing.models import elasticity as el
truth = json.load(open("data/scenarios/axiom_topup_1361_truth.json"))
true_beta = truth["true_elasticity_by_family"]
family_of = {r["product_id"]: r["family"] for r in truth["lines"]}
w = pipeline.run(with_models=False, track=False).waterfall
hits = total = 0
for e in el.fit(w, scope="product", backend="conjugate").estimates:
    if e.beta is None or e.scope["product_id"] not in family_of:
        continue
    t = true_beta[family_of[e.scope["product_id"]]]
    total += 1
    hits += e.low <= t <= e.high
print(f"true elasticity inside the 94% credible interval for {hits} of {total} products")
EOF
```

**78 of 80.** A 94% interval should miss roughly one in seventeen, so a couple of
misses in eighty is the model being honest about its own uncertainty rather than
lucky or overconfident. This is evidence the delivered data cannot provide in
either direction.

---

## What the delivered data can and cannot carry

The evidence for both of these is in
[`notebooks/03_axiom_graph_eda.ipynb`](../notebooks/03_axiom_graph_eda.ipynb),
which explores the data on its own terms — no engine, no models.

**It is wide and shallow.** 100 orders over 78 products and 41 customers, across
20 months. No product was bought more than seven times; the median is three.
Three customers have five or more orders. Anything estimated per product or per
customer has one to seven data points behind it. The 120-document gate reads like
being twenty orders short, but lowering it changes the gate and not the data —
every product scope still has fewer than the eight observations a fit needs.

**Nothing in it responds to price.** List prices never change: every product has
exactly one price period. Every discount is 5%, 10% or 15%, so each product sold
at one, two or at most three distinct prices — and twelve sold at only one, where
no response is estimable even in principle. The graph does contain a randomised
price test, 194 quotes moved by up to 8% — the most valuable thing in the file —
but it is too small to detect a realistic effect and detects nothing (p = 0.084).
Two whole categories of quote, 408 of them, have a win rate of exactly zero, and
win rate rises with negotiating round for mechanical reasons.

The first of those is fixed by more history. The second is a question about how
the extract was produced, and worth putting to whoever produced it before more
time goes into models that read it: *were the quantities and outcomes in this file
generated independently of the prices?*
