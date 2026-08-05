# AXIOM Pricing Module — Track A (Pricing Engine & MLOps)

Week 1 deliverables. Owner: Nourjihen Elabed.

## What Week 1 covers

| Week 1 task (onboarding plan §5) | Where it lives |
|---|---|
| Environment: Python, Neo4j driver, MLflow, DVC, Docker | `requirements.txt`, `docker-compose.yml`, `.env.example` |
| Connect to the read-only graph | `src/pricing/data_source.py`, `src/pricing/cypher.py` |
| Explore the graph: counts, distributions, null rates | `src/pricing/profiling.py`, `scripts/profile_data.py`, `notebooks/01_graph_profiling.ipynb` |
| One-page write-up of the data | `docs/WEEK1_DATA_WRITEUP.md` |
| **Quick win:** all transactions for one customer → gross-to-net | `src/pricing/gross_to_net.py`, `scripts/quick_win_gross_to_net.py` |

## Current data situation

Neo4j credentials have not been issued yet, so everything runs against the
**synthetic CSV snapshot** in `../synthetic/`. This is a placeholder, not a
detour: the code is written against a `GraphSource` interface with two
implementations, and the Cypher for the real graph is already written.

When credentials arrive:

```bash
# option A — read the live graph directly
PRICING_SOURCE=neo4j   # + NEO4J_* in .env

# option B (preferred, and what MLOps needs) — freeze a snapshot first
python scripts/export_neo4j_to_csv.py --out data/snapshot_2026-07-08 --verify
dvc add data/snapshot_2026-07-08
```

`export_neo4j_to_csv.py` writes the *same* filenames and column headers, so
every notebook, script and test keeps working unchanged.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

docker compose up -d        # optional: local Neo4j + MLflow
```

## Run

```bash
python scripts/profile_data.py                        # → reports/profile_report.md
python scripts/quick_win_gross_to_net.py              # biggest customer
python scripts/quick_win_gross_to_net.py --customer CUS-013
python scripts/quick_win_gross_to_net.py --all        # whole book
pytest -q                                             # 10 tests
jupyter lab notebooks/01_graph_profiling.ipynb
```

## Layout

```
src/pricing/
  config.py        backend selection, paths, domain constants
  cypher.py        every Cypher query, read-only by construction
  data_source.py   GraphSource: CsvGraphSource (active) | Neo4jGraphSource (ready)
  profiling.py     counts, null rates, distributions, integrity, reconciliation
  gross_to_net.py  Week 1 waterfall cut — seed of D2
scripts/
  profile_data.py            run the profile, write reports/
  quick_win_gross_to_net.py  the Week 1 quick win
  export_neo4j_to_csv.py     Neo4j → CSV snapshot (for when creds land)
notebooks/01_graph_profiling.ipynb
tests/test_week1.py
docs/WEEK1_DATA_WRITEUP.md
```

## The one rule in this codebase

**Missing is not zero.** A step whose inputs are unknown is reported as
UNAVAILABLE and counted in `flags`; it never becomes 0, and never a partial sum.
Headline totals cover only fully decomposable transactions, with coverage stated
next to them. This is the Brief's "abstain under uncertainty" made mechanical,
and `tests/test_week1.py` enforces it.

## Not in scope this week

Aggregation by arbitrary dimension, IQR outliers, readiness score, segmentation,
elasticity, win-probability, optimiser, MLflow runs, DVC pipeline, Docker
service. Those are Weeks 2–8.
