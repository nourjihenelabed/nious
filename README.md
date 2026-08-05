# AXIOM Pricing Module — Track A (Pricing Engine & MLOps)

Owner: Nourjihen Elabed · engine v0.2.0 · contract v0.2.0-draft · 43 tests

Reads the AXIOM knowledge graph and produces auditable B2B pricing numbers:
a price waterfall from list price to pocket margin, aggregated across any
dimension, handed to Track B as a typed result object carrying its own evidence
and confidence.

## Progress against the plan

| Week | Deliverable | Status |
|---|---|---|
| 1 | Environment, graph profiling, data write-up, gross-to-net quick win | done |
| 2 | Graph-access layer **D1**, module structure, result schema **D8 draft**, MLflow, DVC | done |
| 3 | Price Waterfall Engine **D2** | **done early** — `engine/waterfall.py` |
| 4 | Margin analytics + Pricing Data Readiness Score **D3** | next |
| 5 | Segmentation **D4**, elasticity **D5** | — |
| 6 | Win-probability + optimiser **D6** | — |
| 7–8 | MLOps packaging **D7**, freeze the contract, integrate | — |

Gate documents: [`docs/WEEK1_DATA_WRITEUP.md`](docs/WEEK1_DATA_WRITEUP.md) ·
[`docs/WEEK2_ARCHITECTURE.md`](docs/WEEK2_ARCHITECTURE.md)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

docker compose up -d        # optional: local Neo4j + MLflow UI
```

## Run

```bash
python scripts/run_waterfall.py                     # whole book, all dimensions
python scripts/run_waterfall.py --customer CUS-013  # one customer
python scripts/run_waterfall.py --by segment        # print one aggregation
python scripts/run_waterfall.py --leakage customer  # where is margin leaking?
python scripts/run_profile.py                       # data profile -> reports/

dvc repro                                           # reproduce everything
mlflow ui --backend-store-uri sqlite:///mlflow.db   # inspect runs
pytest -q                                           # 43 tests
```

Everything goes through one entry point:

```python
from pricing.engine import pipeline
run = pipeline.run()          # validate -> waterfall -> aggregate -> contract -> track
run.result.to_json()          # what Track B receives
```

## Layout

```
src/pricing/
  contracts/   engine -> narrative result schema (D8). Track B's only import.
  engine/      waterfall.py · aggregation.py · result.py · pipeline.py
  quality/     profiling.py            (Week 4: readiness score)
  graph/       source.py · cypher.py · validation.py     (D1)
  tracking/    mlflow_runs.py          (Week 7: model registry)
  config.py    backend selection, snapshot resolution, domain constants
scripts/       run_waterfall.py · run_profile.py · export_neo4j_to_csv.py
data/          versioned snapshots (DVC)
notebooks/     01_graph_profiling · 02_waterfall_analysis
tests/         test_graph · test_engine · test_contract
```

Dependencies point inward only: `graph` knows nothing about `engine`, `engine`
knows nothing about `tracking`. `engine/pipeline.py` is the one place they meet.

## Data: CSV now, Neo4j when credentials arrive

Neo4j access has not been issued yet, so everything runs against a synthetic CSV
snapshot. That is a backend choice, not a detour — the code talks to a
`GraphSource` interface with two implementations, and the Cypher for the real
graph is already written and mechanically tested for read-only-ness.

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

**Ratios come from sums.** Never the mean of per-deal percentages — that weights
a €400 order the same as a €400,000 one.

**Aggregates must reconcile.** Every groupby is checked against the grand total
on every run, which catches join fanout before it reaches a report.

**No cross-currency sums.** The graph holds five currencies and no FX rate, so
any aggregate spanning them abstains with a stated reason. Put `currency` in the
grain to get a publishable number.

**Nothing is published without evidence.** Every record in the result contract
carries the IDs it was computed from, plus a confidence score and the reasons
behind it. `contracts.validate()` rejects results that break this.
