# D6 and D7 — win probability, price recommendations, and running them in production

This covers what the onboarding asks for in D6 (a calibrated win-probability model
feeding a constrained expected-profit optimiser) and D7 (MLflow registry, DVC
pipeline, drift monitoring, Docker), how to run and test each part, and what it
cannot do yet.

---

## 1. What D6 produces

For each customer and product, a **recommended price range** (low / mid / high),
with:

- the **chance of winning** the deal at the mid price,
- the **expected profit per unit** at that price,
- a **confidence score** and the reasons behind it,
- the **IDs of the deals and quotes** it was computed from,
- the **rules that shaped it**, if any did.

It comes in two steps.

### Step 1 — the chance of winning at a given price

A logistic regression learned from **past quotes that were won or lost**
(`src/pricing/models/win_probability.py`).

**How price is measured.** Quotes are for different products and quantities, so
their prices cannot share one axis. What they can share is *how far above or
below a reference price the offer was*. In the AXIOM data that is recorded
exactly for two kinds of quote:

- **model-suggested quotes** — the pricing model's baseline price (0% offset);
- **price-test quotes** — the baseline moved by a random offset (−15% to +15%).

Because the test offsets were set at random, the model can tell what price
*causes*, not just what tends to go together. List, contract and negotiated
quotes have no recorded offset, so they are left out of training (and counted
in the report), never guessed.

**What else it uses**, all known on the day the quote went out: the negotiation
round, tender or not, lowest-price-wins tender or not, competitors known at the
time (a competitor learned about later is not used), whether one is the
incumbent, how late our delivery promise is against what was asked, deal size,
customer type, account class, channel and currency.

**What counts as won or lost.** Won = won. Lost = lost to a competitor, or the
customer withdrew after our quote. Deals we did not bid on, and deals still
open, are left out. When a deal was re-bid, only the latest version counts.

**How it is checked.** The model learns from the first 80% of decisions in time
order and is tested on the last 20%, which it never saw. It must pass three
checks before it may set prices:

1. higher prices must lose more deals — and the evidence must be clear (the
   range from 100 refits on resampled deals must stay below zero);
2. its probabilities must match what actually happened, within what chance
   alone explains for that many test deals;
3. it must predict better than "always guess the average win rate".

**On the v2 data:** a 5% higher price lowers the chance of winning by about
10 percentage points (likely 9.6 to 11.5). It passes all three checks.

### Step 2 — the best price, inside the rules

For each customer and product (`recommend_with_win_model` in
`src/pricing/models/optimiser.py`):

1. Take a ladder of 121 prices across the range that was actually tested
   (reference price −15% to +15%).
2. At each price, work out the chance of winning for *this customer*: the
   model is averaged over this customer's past deal situations (their tenders,
   competitors, sizes), not one made-up "typical" deal.
3. Expected profit per unit = (price − unit cost) × chance of winning.
4. Remove every price that breaks a rule:
   - below **cost + 5%** (floor),
   - above **today's list price** (ceiling),
   - with less than a **25% chance of winning** (the volume / retention rule).
5. The best remaining price is the mid. The range is the run of prices around it
   that earn within 2% of the best.

The rules are settings in `pyproject.toml` (`[tool.axiom-pricing.models]`
`margin_floor`, `min_win_probability`), or per run with `PRICING_MARGIN_FLOOR`
and `PRICING_MIN_WIN_PROBABILITY`.

**The trade-off curve** — every price on the ladder with its chance of winning,
profit per unit and expected profit — is saved to `model_tradeoff_curves.csv`.
The expected-profit curve has a single peak: too cheap wins often at a thin
margin, too dear earns a lot per unit but rarely wins.

**The reference price for a product** is the baseline the price tests were
measured from. The data records it only on won model-suggested and price-test
quotes for **single-product orders**: there the quote price ÷ (1 + offset) is
the baseline exactly. Each such quote gives the baseline as a share of that
day's list price; the product's reference is the median share × today's list
price. A product never quoted that way is **not priced**, and says why.

**Confidence** goes down when the customer has fewer than 5 past quotes, when the
product's reference price was seen fewer than 3 times, when a rule had to push
the price, or if the profit curve ever has more than one peak.

### Why not contextual bandits or reinforcement learning

Bandits and reinforcement learning learn by **trying prices** — including prices
they expect to be worse — and watching what happens. That works for frequent,
low-stakes, repeated prices (online ads, retail web prices), where a bad try
costs little and is soon followed by thousands more.

Our deals are the opposite:

- **Few and valuable.** A few thousand quotes a year, each worth a lot. Trying a
  worse price on purpose means losing a real deal or giving away real margin.
- **Too little data to learn by trial.** These methods need far more decisions
  than a B2B book produces before they settle on anything.
- **Hard to explain and audit.** A governance product has to say *why* a price
  was recommended. A fitted win curve plus a visible set of rules can be shown
  and checked; an exploring policy cannot be explained deal by deal.

So we fit a win-probability model on history and optimise against it, under
explicit rules. The one bandit-like idea we do keep is learning from **past**
random price tests — offline, from data that already exists. We never explore
on live deals.

---

## 2. What D7 adds

### MLflow tracking and the model registry

Every `run_models.py` run with tracking on logs the model's test-set scores,
price effect and calibration table, and registers the fitted model as a new
version of `axiom-win-probability-<profile>` with the alias **candidate**.

| alias | meaning |
|---|---|
| `candidate` | the newest version, not yet trusted with prices |
| `champion` | the version production uses |
| `previous_champion` | the one it replaced — kept so rollback is instant |

A candidate is promoted only if it passed its own checks **and** it predicts
deals it never saw at least as well as the champion did (log loss no more than
0.005 worse). `--force` skips the comparison.

### Drift monitoring

`run_monitoring.py` compares the champion with deals decided **after** its
training data ends:

- **Are its probabilities still right?** Predicted against actual win rate, month
  by month and for the last three months together.
- **Do new deals look like the old ones?** A stability score (PSI) for every input:
  under 0.10 stable, 0.10–0.25 moderate change, over 0.25 major change.

**Retrain when** the last three months together, or the whole period, are off by
more than chance explains, or any input changes majorly. The script exits with
code 3 when that happens, so a scheduler can act on it.

On the v2 snapshot there is nothing newer than a model trained on all of it, so
`--backtest` shows the monitor working: train on the first 80%, monitor the rest.
It flags a real change. Over the last three months the model predicted 33% wins
and 44% were won. Model-suggested quotes started winning more often in 2026.

### DVC: one command reproduces everything

The v2 export is versioned in `data/snapshot_axiom_v2` (`data/snapshot_axiom_v2.dvc`).
Four stages in `dvc.yaml` rebuild every v2 result from it:
`readiness_axiom_v2`, `waterfall_axiom_v2`, `models_axiom_v2`, `monitor_axiom_v2`.

### Docker

`Dockerfile` packages the engine with the exact library versions in
`requirements-docker.txt`. Data, reports and the MLflow store are mounted, not
built in. `docker-compose.yml` runs the MLflow UI from the same image (so the
UI and the engine use the same MLflow version) and the engine on demand.
**The image has not been built yet:** Docker is not installed on the
development laptop.

---

## 3. How to run and test it

From `axiom/axiom-pricing-module`, with the virtual environment active
(`axiompricingvenv\Scripts\activate` on Windows).

```bash
# D6, step 1 only: fit the win model and see its report card (fast, nothing saved to MLflow)
python scripts/run_win_model.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph

# D6 complete: every model, price ranges, trade-off curves; registers a candidate in MLflow
python scripts/run_models.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph --out reports_axiom_v2

# D7 registry
python scripts/manage_models.py status   --profile axiom_v2
python scripts/manage_models.py promote  --profile axiom_v2
python scripts/manage_models.py rollback --profile axiom_v2

# D7 monitoring (champion; or --backtest to see it work on this snapshot)
python scripts/run_monitoring.py --profile axiom_v2 --graph ../../axiom_pricing_v2_graph/graph --backtest

# D7 reproduce everything from the versioned snapshot
python -m dvc repro monitor_axiom_v2

# D7 container (needs Docker)
docker build -t axiom-pricing .
docker compose up -d mlflow          # UI on http://localhost:5000
docker compose run --rm engine scripts/run_models.py --profile axiom_v2 --graph data/snapshot_axiom_v2 --out reports_axiom_v2

# tests for all of the above
python -m pytest tests/test_win_model.py
```

`--profile`, `--graph` and `--out` work on every script and replace the
`PRICING_PROFILE`, `PRICING_GRAPH_JSON` and `PRICING_REPORTS_DIR` environment
variables. `python -m dvc` is used because the `dvc.exe` launcher in the
current virtual environment does not start.

### Output files (in the `--out` folder)

| file | what it holds |
|---|---|
| `model_win_report.txt` | the win model's report card, in words |
| `model_win_calibration.csv` | predicted against actual win rate on the test deals, in bands |
| `model_recommendations.csv` | one row per customer and product: range, chance of winning, expected profit, today's price, confidence and reasons, rules that applied, evidence IDs |
| `model_tradeoff_curves.csv` | every price on the ladder: chance of winning, profit per unit, expected profit, allowed or not |
| `monitoring_report.txt`, `monitoring_monthly.csv` | the drift check |

---

## 4. Limits

- **Only 1 in 5 decided quotes has a known price position** (1,802 of 9,834).
  The others are used to describe each customer's deal situations, but not to
  learn the price effect.
- **Lost quotes still have no product.** The model works around this by
  measuring price as an offset, so it predicts *"how much does going 5% above
  the baseline hurt"*, not *"what does product X sell for"*.
- **Reference prices come from single-product orders only.** 11 of the top 200
  customer-product pairs are not priced for this reason.
- **The latest months are under-predicted** (see monitoring). Retraining on all
  of the data, which `run_models.py` does, takes the recent months into account,
  but the next snapshot should be monitored against it.
- **Prices are in the list-price currency (SAR).** Today's price for EUR
  customers is converted at the rate in force on each order date, using the
  rates in the graph.
- **The win model reads price as one effect for every customer type.** The
  dataset README says sensitivity differs by customer archetype. Adding a
  price-by-segment effect is the obvious next step once segments are agreed.
