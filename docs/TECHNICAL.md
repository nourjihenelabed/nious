# AXIOM Pricing Module — Track A (Pricing Engine & MLOps)

Owner: Nourjihen Elabed · engine v0.4.0 · contract v0.2.0-draft · 250 tests

Reads a client's pricing data and produces auditable B2B pricing numbers: a price
waterfall from list price to pocket margin, margin analytics, behavioural
segmentation, elasticity with credible intervals, and constrained price
recommendations — handed to Track B as one typed result object carrying its own
evidence and confidence.

## Progress against the plan

| Week | Deliverable | Status |
|---|---|---|
| 1 | Environment, graph profiling, data write-up, gross-to-net quick win | done |
| 2 | Graph-access layer **D1**, module structure, result schema **D8 draft**, MLflow, DVC | done |
| 3 | Price Waterfall Engine **D2** + hand-computed reference dataset | done |
| 4 | Margin analytics + Pricing Data Readiness Score **D3** | done |
| 5 | Segmentation **D4**, elasticity **D5** | done |
| 6 | Acceptance probability + constrained optimiser **D6** | done |
| 6–7 | Win probability from won **and lost** quotes + constrained optimiser **D6** (v2 data) | done |
| 7 | MLOps **D7**: model registry, DVC pipeline, drift monitoring, Docker | done; Docker image not yet built (no Docker on the dev machine) |
| 8 | The model chain: segments → elasticity, segments → win model, elasticity → optimiser; k chosen by elbow + silhouette | done, see [`docs/CHAIN.md`](CHAIN.md) |
| 8 | The full contract reaches Track B (models included, MLflow run named); demo website | done, see [Demo](#demo) |

**The chain.** Each model now feeds the next, as the plan describes:
segments are chosen from the data (elbow + silhouette, k-means compared with
hierarchical), elasticity is measured inside each segment and checked for
circularity, the win model takes the segment and a price × segment effect, and
the optimiser removes prices the segment elasticity says would lose more than 20%
of volume. [`docs/CHAIN.md`](CHAIN.md) explains each link with the v2 numbers.

## Demo

```bash
python demo/server.py          # then open http://127.0.0.1:8765   (--cache keeps the fitted run between restarts)
```

A customer-facing website on the v2 data, written for people who have never seen
the project. It runs the engine once at start-up (about a minute), then every
view is instant:

| page | what it answers |
|---|---|
| Overview | "Of every 100 SAR on your price list, you keep 22": the list-to-margin bridge, where the money goes, automatic insights, month by month |
| Margin explorer | margin, price kept, money given away and revenue by channel, region, customer type, buying behaviour, industry, account class, product family and product; a channel x region margin heatmap |
| Customers | the behaviour groups in plain words, a customer value map (protect / fix pricing / grow / review), and a profile for every customer |
| Price advisor | the recommended price for a customer and product, why, and a "try your own price" slider that shows win chance, profit and volume against the guardrails |
| Opportunity | what the recommended prices are worth a year on last year's volume, after expected volume changes, by group and channel, price by price |
| How it works | the five steps and the trust story; the technical detail (model chain, checks, contract, MLflow, DVC, drift) sits under "For your data team" |

Filters at the top (channel, region, customer type, buying behaviour, industry,
account class, period) and clicks on any bar, heatmap cell or dot drive every
page. Fully local: no model calls, no internet, no CDN.

Gate documents: [`docs/WEEK1_DATA_WRITEUP.md`](WEEK1_DATA_WRITEUP.md) ·
[`docs/WEEK2_ARCHITECTURE.md`](WEEK2_ARCHITECTURE.md) ·
[`docs/WEEK3_WATERFALL.md`](WEEK3_WATERFALL.md) ·
[`docs/WEEK3_REFERENCE_ARITHMETIC.md`](WEEK3_REFERENCE_ARITHMETIC.md) ·
[`docs/WEEK4_GRAIN_AND_READINESS.md`](WEEK4_GRAIN_AND_READINESS.md) ·
[`docs/WEEK5_SEGMENTATION_AND_ELASTICITY.md`](WEEK5_SEGMENTATION_AND_ELASTICITY.md) ·
[`docs/WEEK6_ACCEPTANCE_AND_OPTIMISATION.md`](WEEK6_ACCEPTANCE_AND_OPTIMISATION.md) ·
[`docs/D6_D7_WIN_MODEL_AND_MLOPS.md`](D6_D7_WIN_MODEL_AND_MLOPS.md) — the win model, price ranges, registry, monitoring, DVC and Docker, and how to test each

Also: [`docs/DATA_INTAKE.md`](DATA_INTAKE.md) — what the engine reads, and
what happens when you swap the files under it ·
[`docs/CONSTANTS.md`](CONSTANTS.md) — every hardcoded number, and whether it
rests on published work, on convention, or on somebody's judgement.

## Setup

```bash
python -m venv axiompricingvenv
source axiompricingvenv/bin/activate        # Windows: axiompricingvenv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

docker compose up -d        # optional: local Neo4j + MLflow UI
```

## Run

```bash
python scripts/check_data.py                        # will this data even run? exits 1 if not
python scripts/run_readiness.py                     # D3: can this data support pricing at all?
python scripts/run_reference_check.py               # D2 acceptance: engine vs hand arithmetic
python scripts/run_waterfall.py                     # whole book, all dimensions
python scripts/run_waterfall.py --customer CUS-013  # one customer
python scripts/run_models.py                        # D3-D6: margin, segments, elasticity, prices
python scripts/run_profile.py                       # data profile -> reports/

dvc repro                                           # reproduce everything
python -m mlflow ui --backend-store-uri sqlite:///mlflow.db   # inspect runs
pytest -q                                           # 243 tests (add -m slow for the MCMC fits and the full v2 chain)
```

Every script writes to `reports/`, which is not keyed by profile. Set
`PRICING_REPORTS_DIR` when running a second client's data so it does not land on
top of the first one's numbers.

Everything goes through one entry point:

```python
from pricing.engine import pipeline
run = pipeline.run(with_models=True)   # validate -> waterfall -> analytics -> models -> contract
run.result.to_json()                   # what Track B receives
```

## Onboarding a new client

Onboarding is writing two declarations, not forking the engine.

**A profile** (`pricing/spec.Profile`) declares the waterfall: an ordered chain of
levels and deductions, each read from the data or derived from its neighbours,
each carrying the grain at which it is *known*. Adding a concession type is
adding a `Step`. Register it and select it with `PRICING_PROFILE`.

**A source mapping** (R2RML-style JSON) says which spreadsheet column becomes
which graph node. `pricing/mapping/` reads a workbook through it, and — when the
mapping arrives before the workbook does — runs it backwards to generate a
fixture with exactly the structure it declares:

```bash
python scripts/make_mapping_fixture.py --mapping ../axiom-source-mapping.json
PRICING_SOURCE=workbook PRICING_PROFILE=dairy_al_kharj python scripts/run_readiness.py
```

Three profiles ship: `b2b_industrial` (synthetic distributor snapshot),
`dairy_al_kharj` (the Al Kharj dairy model, read through its source mapping) and
`axiom_poc` (the delivered AXIOM knowledge graph).

### The AXIOM knowledge graph

The graph arrived as a JSON export rather than as Bolt credentials. It is read
**in place** — there is no import step and no CSV on the path from the graph to
an answer:

```bash
export PRICING_PROFILE=axiom_poc
export PRICING_REPORTS_DIR=reports_axiom_poc   # reports/ is not keyed by profile

python scripts/check_data.py ../axiom_pricing_module_Knowledge-graph_PoC.json
python scripts/run_readiness.py
python scripts/run_waterfall.py
python scripts/run_models.py
```

No `PRICING_SOURCE` and no `PRICING_DATA_DIR`. The profile declares
`default_source="graph_json"`, so the engine reads the export the way it will
read Bolt: a `Source` serving named logical tables, where a table *is* a node
label. The day credentials arrive, `PRICING_SOURCE=neo4j` is the whole change.

This is the first source that records its own waterfall: `PriceComponent` nodes
carry the step name, the on/off-invoice band and the grain the concession was
posted at, so the profile transcribes a chain instead of inferring one. It is
also the first with concessions posted at a grain **coarser than the document** —
rebates accrued against a customer-quarter, which span orders outside the book.
Those are excluded from the waterfall and reported beside it, because allocating
them down to an order is exactly the thing this engine does not do.

**Integrity is checked on every read.** This export states most foreign keys
twice — once as an edge, once as a denormalised property, so `OrderLine.order_id`
sits beside `(:OrderLine)-[:PART_OF]->(:Order)`. The profile reads the property
because it is cheaper, which makes a disagreement a wrong number with a
clean-looking audit trail. All 7,402 of them are checked at the entry gate, along
with the export's own node and edge counts, whether every edge lands on a node
that is present, and whether every business key is unique. A graph that
contradicts itself is refused, not reported.

Freezing a snapshot is still worth doing, but for provenance rather than for
reading — a DVC content hash you can put beside an MLflow run and return to:

```bash
python scripts/import_graph_json.py --verify && dvc add data/snapshot_axiom_poc_v1
```

The snapshot is one CSV per node label, read by the same profile, and it must
produce the same totals to the cent — `tests/test_axiom_poc.py` asserts that.

### Three scenarios, one engine

What the module computes is a property of the data, not of a switch. The way to
show that is to hold the code still and move the data underneath it:

```bash
python scripts/scenarios.py
```

A menu over the same graph in three states, running the same four scripts each
time. [`docs/COMMANDS.md`](COMMANDS.md) has every command it runs.

| | 1. as arrived | 2. reduced | 3. topped up |
|---|---|---|---|
| orders | 100 | 12 | 1,361 |
| median lines per product | 3 | 1 | 43 |
| readiness | 78.8 LIMITED | 72.0 LIMITED | 86.3 READY |
| elasticity | BLOCKED | BLOCKED | READY |

Scenario 3 keeps the delivered graph whole and adds trade in the same catalogue
until every gate opens. **It takes 1,261 added orders, not twenty** — the book is
100 orders over 78 products, and elasticity is fitted per product, so what is
missing was never a handful of documents. The added orders carry a true elasticity
the engine is never told, and it recovers it inside the 94% credible interval for
78 of 80 products.

[`docs/SCENARIOS.md`](SCENARIOS.md) — the three in detail ·
[`docs/COMMANDS.md`](COMMANDS.md) — every command, including MLflow and DVC ·
[`notebooks/03_axiom_graph_eda.ipynb`](notebooks/03_axiom_graph_eda.ipynb) — the
data on its own terms

Before running anything on data you did not produce, check it fits:

```bash
python scripts/check_data.py --identify data/incoming   # which profile is this?
python scripts/check_data.py --describe                 # what must the data look like?
```

A renamed column does not raise — pandas returns the frame, the merge yields NaN,
and the engine faithfully reports a smaller book. `check_data.py` catches that,
and the number-exported-as-text case that is quieter still. See
[`docs/DATA_INTAKE.md`](DATA_INTAKE.md).

## Layout

```
src/pricing/
  spec.py        Profile: the declared step chain, its grain, its dimensions
  profiles.py    the shipped profiles, one per source schema
  contracts/     engine -> narrative result schema (D8). Track B's only import.
  engine/        waterfall.py · aggregation.py · result.py · pipeline.py
  analytics/     margin.py                          (D3: leakage, bands, quadrants)
  quality/       readiness.py · conformance.py · profiling.py   (D3: the admission test)
  models/        segmentation.py · elasticity.py · hierarchical.py
                 acceptance.py · optimiser.py          (D4-D6)
  mapping/       r2rml.py · workbook.py · fixture.py   (schema mapping layer)
  graph/         source.py · json_source.py · cypher.py · validation.py   (D1)
  tracking/      mlflow_runs.py                     (Week 7: model registry)
  config.py      backend selection, snapshot resolution, domain constants
scripts/         check_data · run_readiness · run_waterfall · run_models · run_profile
                 run_reference_check · make_mapping_fixture · export_neo4j_to_csv
                 import_graph_json  (freeze the graph for DVC; not on the read path)
                 scenarios          (the menu: three datasets, four steps, explained)
                 make_scenario      (builds the reduced and topped-up datasets)
data/            versioned snapshots (DVC)
tests/           test_graph · test_engine · test_contract · test_reference
                 test_spec · test_mapping · test_models · test_conformance
                 test_hierarchical · test_axiom_poc
tests/reference/ the hand-computed D2 oracle: fixture + expected answers
```

Dependencies point inward only: `graph` knows nothing about `engine`, `engine`
knows nothing about `tracking`. `engine/pipeline.py` is the one place they meet.

## Data: CSV now, Neo4j when credentials arrive

Neo4j access has not been issued yet, so everything runs against a frozen CSV
snapshot. That is a backend choice, not a detour — the code talks to a `Source`
interface with three implementations (CSV, Neo4j, mapped workbook), and the
Cypher for the real graph is already written and mechanically tested for
read-only-ness.

When credentials arrive:

```bash
# option A — read the live graph directly
PRICING_SOURCE=neo4j        # + NEO4J_* in .env

# option B (preferred, and what reproducibility needs) — freeze a snapshot
python scripts/export_neo4j_to_csv.py --out data/snapshot_2026-07-08 --verify
dvc add data/snapshot_2026-07-08
```

The exporter writes the same filenames and column headers, and `config.py`
automatically picks the newest `data/snapshot_*`, so every notebook, script and
test keeps working unchanged — now on real data.

## The rules this codebase enforces

**Missing is not zero.** A step whose inputs are unknown is UNAVAILABLE and
counted in `flags`; it never becomes 0, and never a partial sum. Headline totals
cover only fully decomposable transactions, with coverage stated beside them.

**A partial sum is not a result.** A step is summed only when every contributing
row has it. Half an order's COGS presented as the order's COGS is worse than a
visible gap, because nothing about it looks wrong.

**Nothing is allocated.** A measure recorded against the order is reported
against the order. Slicing it by something that varies inside an order — a
product, a category — returns UNAVAILABLE with the grain stated. Rolling *up* is
free of assumptions; rolling down is not. This replaced the pro-rata allocation
the engine used to do, and cost nothing above product grain: see
[`docs/WEEK4_GRAIN_AND_READINESS.md`](WEEK4_GRAIN_AND_READINESS.md).

**Ratios come from sums.** Never the mean of per-deal percentages — that weights
a €400 order the same as a €400,000 one. And a ratio with a zero denominator does
not exist: it is UNAVAILABLE, never ±infinity.

**Aggregates must reconcile.** Every groupby is checked against the grand total
on every run, which catches join fanout before it reaches a report.

**No cross-currency sums.** The graph holds five currencies and no FX rate, so
any aggregate spanning them abstains with a stated reason. Put `currency` in the
grain to get a publishable number.

**A model that cannot be fitted is not fitted.** The readiness score decides what
runs. Thin groups abstain rather than receiving a confident label; an elasticity
whose interval crosses zero — or whose chains did not converge — is barred from
optimisation; an acceptance estimate built from won deals only can never be
reported as high confidence.

**Nothing is published without evidence.** Every record in the result contract
carries the IDs it was computed from, plus a confidence score and the reasons
behind it. `contracts.validate()` rejects results that break this.

**The arithmetic is checked against a hand computation.** `tests/reference/` is an
eleven-transaction fixture whose every step was worked out on paper before the
engine saw it. 279 comparisons, exact agreement.
