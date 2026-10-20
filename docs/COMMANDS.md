# Every command, and what it is for

Owner: Nourjihen Elabed · engine v0.4.0

Nothing here needs memorising. **Section 1 is the whole thing** — one command,
a menu, three scenarios. The rest of this file is what that menu runs underneath,
written out so nothing is hidden, plus the tooling around it (MLflow, DVC, tests).

---

## 0. Once, at the start of a session

```bash
cd axiom-pricing-module
source axiompricingvenv/Scripts/activate
```

On Windows PowerShell that last line is `axiompricingvenv\Scripts\activate`.

Check it worked — every command below assumes this:

```bash
python -c "import pricing, pandas; print('ok')"
```

---

## 1. The menu — start here

```bash
python scripts/scenarios.py
```

```
==============================================================================
  AXIOM PRICING MODULE -- SCENARIOS
==============================================================================

    1   Data as it arrived                 [ready]
    2   Data reduced                       [not built]
    3   Data topped up until it passes     [not built]

    c   Compare all three
    b   Build the datasets for 2 and 3
    q   Quit
```

Press **b** the first time to build datasets 2 and 3 (about a minute), then pick
a scenario. It asks which of the four steps to run; **a** runs all four in order,
which is the right answer the first time.

It runs the four scripts in order and prints nothing of its own beyond a header
per step. What each step does is section 3 below.

### Without the menu

```bash
python scripts/scenarios.py 1              # scenario 1, all four steps
python scripts/scenarios.py 2 waterfall    # scenario 2, just the waterfall
python scripts/scenarios.py 3 models       # scenario 3, just the models
python scripts/scenarios.py compare        # all three, one table
```

Step names: `check`, `readiness`, `waterfall`, `models`.

Each scenario writes to its own folder — `reports_scenario_1/`,
`reports_scenario_2/`, `reports_scenario_3/` — so one never lands on top of
another.

---

## 2. What the three scenarios are

All three are the same knowledge graph in three states. The engine, the profile
and the backend do not change between them.

| | 1. as it arrived | 2. reduced | 3. topped up |
|---|---|---|---|
| orders | 100 | 12 | 1,361 |
| order lines | 262 | 38 | 3,462 |
| median lines per product | 3 | 1 | 43 |
| currencies | 2 | 1 | 2 |
| coverage | 100% | 42% | 100% |
| readiness | 78.8 LIMITED | 72.0 LIMITED | 86.3 READY |
| waterfall | READY | LIMITED | READY |
| margin analytics | LIMITED | BLOCKED | LIMITED |
| segmentation | BLOCKED | BLOCKED | READY |
| elasticity | BLOCKED | BLOCKED | READY |
| optimisation | BLOCKED | BLOCKED | LIMITED |

**1 — as it arrived.** The delivered export, untouched.

**2 — reduced.** 12 of those orders, with a quarter of the product cost records
removed. Two different kinds of damage on purpose: the book gets *shorter*
(margin analytics wants 20 documents and now has 12) and it gets *holes* (COGS
cannot be computed on 9 of 38 lines, so 7 of 12 orders leave the certified set).
The engine reports those two differently, which is the thing to watch.

**3 — topped up until it passes.** The delivered export kept whole — all 100 real
orders still there — plus added trade in the same catalogue, at the same real
list prices and costs, until every gate opens.

That third one answers a specific question: *what would have to be true of this
data for the pipeline to run end to end?* The answer it prints is **1,261 orders
had to be added**. Not twenty. The delivered book is 100 orders over 78 products,
and elasticity is fitted per product, so what was missing was never a handful of
documents — it was depth in every product, and a price that moves.

Note the currency row. Scenario 3 still has two currencies, because trading more
does not supply an exchange rate. Volume problems are fixable by volume; that one
is not.

### Building the datasets by hand

```bash
python scripts/make_scenario.py reduced
python scripts/make_scenario.py topup
```

Both are deterministic — same seed, same file — so any number in a report traces
back to a dataset you can rebuild. Neither touches the delivered export. Both
write to `data/scenarios/`, which is gitignored because the scripts are the
artefact worth versioning, not a 24 MB JSON.

Variants, if you want to move one lever at a time:

```bash
python scripts/make_scenario.py reduced --orders 12 --drop-cost-periods 0    # shorter, but whole
python scripts/make_scenario.py reduced --orders 40 --drop-cost-periods 0.5  # longer, but holed
python scripts/make_scenario.py topup --months 10                            # a smaller top-up
python scripts/make_scenario.py topup --lines-per-month 1                    # a thinner one
```

`topup` also writes a truth file — the parameters the added orders were generated
from, including a true price elasticity per product family that the engine is
never told. Section 6 uses it.

---

## 3. The four steps, run by hand

The menu sets three environment variables and then runs four scripts. Here are
both halves.

```bash
export PRICING_PROFILE=axiom_poc
export PRICING_GRAPH_JSON=../axiom_pricing_module_Knowledge-graph_PoC.json
export PRICING_REPORTS_DIR=reports_scenario_1
```

- `PRICING_PROFILE` — which schema to read. `axiom_poc` is the knowledge graph.
- `PRICING_GRAPH_JSON` — which export. Swap this line to change scenario.
- `PRICING_REPORTS_DIR` — where output goes. `reports/` is not keyed by profile,
  so without this the second scenario overwrites the first one's files.

For scenarios 2 and 3, change only the middle line:

```bash
export PRICING_GRAPH_JSON=data/scenarios/axiom_reduced_12_gaps25.json
export PRICING_GRAPH_JSON=data/scenarios/axiom_topup_1361.json
```

### Step 1 — will this data run at all?

```bash
python scripts/check_data.py
```

Checks the data has the tables and columns the profile reads, and that its
numbers parse as numbers. Catches the quiet failure: a renamed column does not
raise an error, it produces blanks, and the engine then faithfully reports a
smaller book that looks entirely reasonable. Exits 1 when the data would not run,
so it can gate a CI job.

```bash
python scripts/check_data.py --describe               # what the profile requires
python scripts/check_data.py --identify data/incoming # which profile does this look like?
python scripts/check_data.py --write                  # also save the report
python scripts/check_data.py some/other/file.json     # check a specific file
```

### Step 2 — what can this data support?

```bash
python scripts/run_readiness.py
```

Scores the dataset out of 100 on five checks — structure, chain, grain, integrity
and depth — then says READY, LIMITED or BLOCKED for each deliverable, and names
what would fix it. **This is the step that decides what runs.**

```bash
python scripts/run_readiness.py --all       # every registered profile
```

### Step 3 — the price waterfall

```bash
python scripts/run_waterfall.py
```

The ladder from list price down to pocket margin, for the whole book and sliced
by every dimension the profile declares. Arithmetic, not modelling — nothing is
estimated here. Also prints what it could not compute and why, and checks every
slice adds back to the grand total.

```bash
python scripts/run_waterfall.py --customer CUST0009   # one customer
```

### Step 4 — the models

```bash
python scripts/run_models.py
```

Four models, in order, each gated by the readiness score. A model the data cannot
support is skipped with a stated reason rather than fitted badly.

**1. Margin analytics** — where the money leaks. Which concessions cost most,
which customers are large but thin, how widely the same product sells at
different prices. Descriptive: counting, not predicting. Needs 20 documents.

**2. Segmentation** — groups customers by *how they buy*, not by who they are:
discount depth, price realisation, how often and how broadly they order. K-means
on those behaviours. Needs customers with enough orders to have a habit — 5 each,
and at least 4 such customers for 4 clusters.

**3. Elasticity** — how much volume moves when price moves. A log-log regression
of quantity on price, fitted per product and pulled toward the book-wide average
in proportion to how little each product's own data says. Always reported with a
credible interval; an interval that crosses zero is barred from step 4. Needs 120
documents, and at least 8 observations in each product scope.

**4. Acceptance + optimiser** — how likely a price is to be agreed, and the best
price given that. The optimiser searches a price band under constraints and
returns a recommendation with its evidence. It multiplies elasticity by
acceptance, so it only runs where both are trustworthy. Needs 200 documents.

### Also available

```bash
python scripts/run_profile.py            # column-by-column data profile
python scripts/run_reference_check.py    # engine arithmetic vs the hand computation
python scripts/build_lineage.py          # the lineage diagram in docs/
```

---

## 3b. Did it pass?

Every script ends with its own verdict line:

```
Pipeline: PASS
```

and exits 0 for PASS, 1 for FAIL, 2 when the data was refused at the entry gate.
So this gates CI directly:

```bash
python scripts/scenarios.py 1 && echo "pipeline healthy"
```

**PASS/FAIL is about the code. READY/LIMITED/BLOCKED is about the data.** They are
independent, and scenario 1 is PASS with three deliverables BLOCKED — which is the
module working as designed, not breaking.

A run **FAILS** only when the code produced something it should not have:

- a **contract violation** — a published record missing its evidence or confidence
- a **reconciliation failure** — a slice that does not add back to the grand total
- a **blocking data finding** — the entry gate refusing a graph that contradicts
  itself (a duplicate key, a dangling edge, a foreign key disagreeing with the
  relationship beside it)

A deliverable being BLOCKED is none of those. It is the engine declining to
publish a number the data cannot support.

The same information is written as JSON for anything that reads it mechanically:

```bash
cat reports_scenario_1/pipeline_status.json
```

```json
{
  "ok": true,
  "contract": { "records": 9, "violations": [] },
  "reconciliation": { "dimensions_checked": 6, "dimensions_failed": [] },
  "validation": { "blocking": [], "warnings": ["multiple currencies: ..."] },
  "book": { "documents": 100, "certified": 100, "coverage_pct": 100.0 },
  "mlflow_run_id": "4cc6ac6fe4034c40a4bf5c51b3e16ba7"
}
```

### Seeing a failure on purpose

To check the FAIL path is real rather than taking it on trust, break a graph in
the one way the entry gate exists to catch — a foreign key that disagrees with
the relationship beside it:

```bash
python - <<'EOF'
import json, pathlib
src = pathlib.Path("data/scenarios/axiom_reduced_12_gaps25.json")
doc = json.loads(src.read_text(encoding="utf-8"))
broken = 0
for node in doc["nodes"]:
    if node["labels"][0] == "OrderLine" and broken < 4:
        node["properties"]["order_id"] = "ORD99999"   # points at nothing
        broken += 1
pathlib.Path("data/scenarios/axiom_broken.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
print(f"broke {broken} order lines")
EOF

PRICING_PROFILE=axiom_poc PRICING_GRAPH_JSON=data/scenarios/axiom_broken.json PRICING_REPORTS_DIR=reports_broken python scripts/run_waterfall.py
echo "exit code: $?"
```

It refuses, names both problems, and exits 2:

```
Refusing to run on this snapshot.
  [BLOCKING] contradictory key: OrderLine.order_id disagrees with
             -[:PART_OF]->Order.order_id on 4 of 38 edges
  [BLOCKING] referential integrity: order line -> order has unresolved
             references, e.g. ['ORD99999']
exit code: 2
```

`run_models.py` refuses the same graph with the same exit code. Clean up with
`rm data/scenarios/axiom_broken.json && rm -rf reports_broken`.

---

## 4. Reading the output

Each scenario writes a folder. The files worth opening first:

| File | What it holds |
|---|---|
| `readiness_axiom_poc.md` | the score, the five checks, what each deliverable needs |
| `waterfall_totals.csv` | the ladder, whole book |
| `waterfall_availability.csv` | per step: what could and could not be computed |
| `waterfall_flags.csv` | data-quality findings, each with its blast radius |
| `waterfall_by_*.csv` | the ladder by customer, product, channel, region, period, segment |
| `validation.txt` | the entry-gate findings |
| `margin_*.csv` | leakage bridge, quadrants, price bands |
| `model_elasticity.csv` | per product: slope, interval, evidence, whether it is trusted |
| `model_segments.csv` | which customer is in which segment |
| `model_recommendations.csv` | recommended prices with acceptance and evidence |
| `engine_result.json` | the typed contract — what the downstream team receives |

```bash
ls reports_scenario_1/
cat reports_scenario_1/readiness_axiom_poc.md
python -c "import pandas as pd; print(pd.read_csv('reports_scenario_1/waterfall_totals.csv').to_string())"
```

---

## 5. MLflow — the run history

Every `run_waterfall.py` and `run_models.py` run records itself: its metrics, its
parameters, the data it read and a content hash of that data. That is what makes
a number from three weeks ago traceable.

The store is a local SQLite file, `mlflow.db`. Nothing leaves the machine.

```bash
python -m mlflow ui --backend-store-uri sqlite:///mlflow.db
```

Leave that terminal running — it is a web server, so it does not return to the
prompt. Give it 10–20 seconds on the first start, then open:

**<http://127.0.0.1:5000>**

Ctrl-C in the terminal stops it.

> **Use `python -m mlflow`, not bare `mlflow`.** The `mlflow.exe` shim in this
> virtual environment is broken — it exits silently with status 1 and prints
> nothing, which looks exactly like the command having worked. `python -m mlflow`
> runs the same code and works. Same for `python -m mlflow --version` if you want
> to check the install.

### Finding your runs in the UI

MLflow 3 opens on a **GenAI overview** page that will be empty — that is the
wrong page, not a problem with your data. To get to the pricing runs:

1. Click **axiom-pricing-engine** under Recent Experiments on the home page.
2. In the left sidebar under *Model training*, click **Runs** — or go straight to
   <http://127.0.0.1:5000/#/experiments/1/runs>.

You should see runs named `waterfall` (from `run_waterfall.py`) and `models`
(from `run_models.py`), each tagged with the `dataset` it read. Tick two or three
and press **Compare** to put them side by side.

If the run table is cramped, close the **MLflow Assistant** panel on the right
with the × in its corner.

### What each run records

Three groups, answering three different questions.

**Did it work?** `pipeline_ok` (1 or 0), `contract_violations`,
`reconciliation_failures`, `validation_blocking`, `validation_warnings`. Tag
`ok` carries the same verdict for filtering.

**What could the data carry?** `readiness_score` out of 100, the five checks
behind it (`readiness__structure`, `__chain`, `__grain`, `__integrity`,
`__depth`), and one entry per deliverable — as a tag (`can__elasticity` =
`READY`/`LIMITED`/`BLOCKED`, for grouping) and as a metric (2/1/0, for plotting
over time).

**What did the models conclude?** `elasticity_scopes_fitted`,
`elasticity_scopes_abstained`, `elasticity_scopes_trusted_for_pricing`,
`elasticity_prior_mean`, `segments_found`, `segment_customers_abstained`,
`acceptance_scopes`, `price_recommendations`, `models_skipped`. Anything skipped
is tagged `skipped__<name>` with the reason.

**What the book was.** Every waterfall step as a metric, plus `n_transactions`,
`n_certified`, `coverage_pct`, per-step `available_pct__*` and per-flag
`flag_txn__*`.

Artifacts per run: `waterfall_totals.json`, `data_quality_flags.json`,
`step_availability.json`, `readiness_capabilities.json`, the aggregation CSVs and
the result contract.

### The comparison that matters

Run all three scenarios, then select their three `models` runs and Compare. The
columns to add:

| | 1. as arrived | 2. reduced | 3. topped up |
|---|---|---|---|
| `n_transactions` | 100 | 12 | 1,361 |
| `readiness_score` | 78.8 | 72.0 | 86.3 |
| `can__elasticity` | BLOCKED | BLOCKED | READY |
| `elasticity_scopes_trusted_for_pricing` | — | — | 61 |
| `segments_found` | — | — | 4 |
| `price_recommendations` | — | — | 200 |
| `pipeline_ok` | 1 | 1 | 1 |

That last row is the point worth making out loud: all three runs succeeded. What
differs is what the data could carry.

Without the browser:

```bash
python -c "
import mlflow, pandas as pd
pd.set_option('display.width', 220)
mlflow.set_tracking_uri('sqlite:///mlflow.db')
df = mlflow.search_runs(experiment_names=['axiom-pricing-engine'], order_by=['start_time DESC']).head(6)
cols = {'tags.mlflow.runName':'run', 'tags.dataset':'dataset',
        'metrics.n_transactions':'orders', 'metrics.readiness_score':'readiness',
        'tags.can__elasticity':'elasticity',
        'metrics.elasticity_scopes_trusted_for_pricing':'trusted',
        'metrics.price_recommendations':'recs', 'metrics.pipeline_ok':'ok'}
have = {k: v for k, v in cols.items() if k in df.columns}
print(df[list(have)].rename(columns=have).to_string(index=False))
"
```

Turn tracking off for a throwaway run with `--no-track` on either script.

### Without the browser

```bash
python -c "
import mlflow, pandas as pd
mlflow.set_tracking_uri('sqlite:///mlflow.db')
runs = mlflow.search_runs(experiment_names=['axiom-pricing-engine'])
cols = ['start_time', 'tags.mlflow.runName', 'metrics.n_transactions',
        'metrics.coverage_pct', 'metrics.pocket_margin_pct']
print(runs[cols].head(20).to_string())
"
```

The artefacts each run saved (`waterfall_totals.json`, `data_quality_flags.json`,
`step_availability.json`) are under `mlartifacts/<run_id>/artifacts/`.

```bash
ls mlartifacts/
```

If the UI will not start because the port is busy, use another one:

```bash
python -m mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5001
```

---

## 6. Checking the engine against a known answer

Two independent checks that the arithmetic and the modelling are right.

### The waterfall against a hand computation

```bash
python scripts/run_reference_check.py
```

`tests/reference/` is an eleven-transaction fixture whose every waterfall step was
worked out on paper before the engine saw it. 279 comparisons, exact agreement.

### Elasticity against a hidden truth

The added orders in scenario 3 were generated with a real price elasticity per
product family, which the engine is never told. Recovering it is the acceptance
test:

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

Expect **78 of 80**. A 94% interval should miss roughly one in seventeen, so a
couple of misses in eighty is the model being honest about its own uncertainty
rather than lucky or overconfident.

---

## 7. The test suite

Nothing in the scenario work changes the engine, so the existing tests must still
pass.

```bash
pytest -q                            # 216 tests, about a minute
pytest -q -m slow                    # including the MCMC fits (several minutes)
pytest tests/test_axiom_poc.py -q    # the knowledge graph specifically
pytest tests/test_reference.py -q    # arithmetic vs the hand computation
pytest tests/test_engine.py -q       # the waterfall engine
pytest tests/test_models.py -q       # segmentation, elasticity, acceptance, optimiser
pytest -q -k currency                # anything matching a keyword
pytest -q --lf                       # just what failed last time
```

Linting:

```bash
ruff check src tests scripts
ruff check --fix src tests scripts
```

---

## 8. DVC — reproducing the whole pipeline

`dvc.yaml` declares the five stages as a dependency graph, so DVC reruns only what
the inputs changed under.

```bash
dvc repro            # run whatever is out of date
dvc repro --force    # run everything regardless
dvc status           # what is stale, and why
dvc dag              # the stage graph
```

Pinning a dataset by content hash, so a report can be reproduced later:

```bash
python scripts/import_graph_json.py --verify   # freeze the graph as one CSV per label
dvc add data/snapshot_axiom_poc_v1
```

That is for provenance, not for reading — the engine reads the graph export in
place. The frozen snapshot must produce the same totals to the cent, and
`tests/test_axiom_poc.py` asserts it.

---

## 9. The notebooks

```bash
jupyter lab
```

| Notebook | What it is |
|---|---|
| `01_graph_profiling.ipynb` | Week 1 profiling of the synthetic CSV snapshot |
| `02_waterfall_analysis.ipynb` | Week 2 waterfall on that same snapshot |
| `03_axiom_graph_eda.ipynb` | **exploratory analysis of the delivered knowledge graph** |

Notebook 03 is the one about the delivered data: what arrived, how it connects,
what is blank, how deep it goes, what the prices and quantities look like, and
whether anything in it responds to price. No engine, no models — just the data.

It ships with its outputs saved, so it can be read without running anything. To
re-run it headlessly:

```bash
python -m nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.timeout=600 notebooks/03_axiom_graph_eda.ipynb
```

---

## 10. Other profiles

The module reads three schemas. The scenario work above is all `axiom_poc`.

```bash
PRICING_PROFILE=b2b_industrial python scripts/run_waterfall.py   # synthetic CSV snapshot
PRICING_PROFILE=dairy_al_kharj python scripts/run_readiness.py   # the Al Kharj dairy model
python scripts/check_data.py --describe --profile dairy_al_kharj # the schema it needs
```

---

## 11. Overriding the gates

The thresholds live in `pyproject.toml` and can be overridden per run. Doing so is
legitimate; doing it silently is not, which is why anything set below the
recommended value is listed by `run_readiness.py` under *Thresholds lowered for
this run* and attached to every estimate fitted under it.

```bash
PRICING_MIN_ELASTICITY=100 python scripts/run_models.py     # fit on 100 documents, not 120
PRICING_ELASTICITY_SCOPE=family python scripts/run_models.py # fit per family, not per product
PRICING_SEGMENT_MIN_DEALS=3 python scripts/run_models.py     # cluster thinner customers
```

The one worth running on the delivered data, to see what is actually there:

```bash
export PRICING_PROFILE=axiom_poc
export PRICING_GRAPH_JSON=../axiom_pricing_module_Knowledge-graph_PoC.json
export PRICING_REPORTS_DIR=reports_scenario_1

PRICING_ELASTICITY_SCOPE=family PRICING_MIN_ELASTICITY=100 python scripts/run_models.py
```

Both variables are needed — the document gate blocks the fit before the scope
matters. All six family slopes come out **positive** (demand apparently rising
with price), R-squared between 0.003 and 0.13, every credible interval crossing
zero, and all six marked `fit_for_optimisation = False`. The engine fits, finds
nothing, and says so per scope:

```
the credible interval [-0.71, 1.10] includes zero or a positive slope,
so demand may not respond to price here at all
```

---

## 12. When something goes wrong

**`Conformance: UNUSABLE ... Missing tables: orders expected Order.csv`**
`PRICING_PROFILE` is not `axiom_poc`, so a CSV profile is being pointed at a graph
export. Export it and re-run.

**`Graph export not found`**
`PRICING_GRAPH_JSON` is relative to `axiom-pricing-module/`. The delivered export
is one level up: `../axiom_pricing_module_Knowledge-graph_PoC.json`.

**Numbers from the wrong scenario**
`PRICING_REPORTS_DIR` was not set, so the run landed in `reports/` on top of
something else. Use the menu, which sets it, or set it every time.

**`No module named 'pricing'`**
The virtual environment is not active, or you are not in `axiom-pricing-module/`.

**A scenario file does not exist**
Rebuild it: `python scripts/make_scenario.py reduced` or `... topup`. The order
count is in the file name and comes out of the generator, so a top-up lands at
`axiom_topup_1361.json` rather than a round number.

**`run_models.py` skips almost everything**
That is the answer, not a fault. `run_readiness.py` says what each skipped
deliverable would need.

**MLflow UI will not start, and prints nothing at all**
You ran bare `mlflow`. The `mlflow.exe` shim in this virtual environment is
broken: it exits 1 silently, which is indistinguishable from the command having
worked. Use `python -m mlflow ui --backend-store-uri sqlite:///mlflow.db`.

**MLflow UI starts but the page is empty**
MLflow 3 opens on a GenAI overview page that has nothing to do with these runs.
Click **axiom-pricing-engine**, then **Runs** in the left sidebar — or go to
<http://127.0.0.1:5000/#/experiments/1/runs>.

**MLflow UI will not bind**
Port 5000 is busy. Use `--port 5001` and open <http://127.0.0.1:5001>.

**A script prints `Pipeline: FAIL`**
Scroll up. A blocking data finding means the entry gate refused the graph, and
the findings above name the rows involved. A contract violation or a
reconciliation failure means the engine produced something it should not have;
that is a bug, not a data problem. `pipeline_status.json` has the same detail in
a form you can read mechanically.

**A scenario says PASS but everything is BLOCKED**
That is the expected result for scenario 1, and it is not a failure. PASS means
the code ran correctly. BLOCKED means the data cannot carry that deliverable. The
reason printed beside each one is the thing to act on.
