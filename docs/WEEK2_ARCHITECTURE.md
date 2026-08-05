# Week 2 — Architecture review

**AXIOM Pricing Module · Track A (Pricing Engine & MLOps)** · Nourjihen Elabed
Phase 0 gate · engine v0.2.0 · contract v0.2.0-draft
Snapshot `data/snapshot_synthetic_v1`, sha256 `6af98ff70daf16a1…` · 700 transactions · 43 tests passing

---

## 1. What this gate is meant to show

> *Gate — Architecture review: data access working, engine skeleton + result schema agreed, waterfall design validated.*

| Required | Status |
|---|---|
| Data access working (D1) | Done, plus a refuse-to-run validation gate |
| Engine module structure | Done — layered package, dependencies point one way |
| Result-object schema drafted (D8) | Done — typed, with evidence, confidence and status |
| Waterfall design validated | Done, and **implemented** — D2 pulled forward from Week 3 |
| MLflow tracking | Done — every run logged, keyed to the snapshot hash |
| DVC versioning of the snapshot | Done — `dvc repro` reproduces the whole pipeline |

D2 arriving a week early is deliberate: the waterfall is the most certain ground
in the whole module, and having real aggregated numbers made the two significant
data findings below surface now rather than in Week 4.

## 2. Module structure

```
src/pricing/
  contracts/   the engine -> narrative result schema (D8). Track B's only import.
  engine/      waterfall.py · aggregation.py · result.py · pipeline.py
  quality/     profiling.py  (Week 4: the Pricing Data Readiness Score)
  graph/       source.py · cypher.py · validation.py     (D1)
  tracking/    mlflow_runs.py  (Week 7: the model registry)
  config.py    backend selection, snapshot resolution, domain constants
```

**Dependencies point inward only.** `graph` knows nothing about `engine`;
`engine` knows nothing about `tracking`. `engine/pipeline.py` is the single
place they meet, and the single entry point every script and notebook calls.
The practical payoff is that Week 5's segmentation and elasticity modules drop
into `engine/` without touching data access, and Week 7's containerisation wraps
one function.

Three renames from Week 1, for clarity: `data_source.py` → `graph/source.py`,
`gross_to_net.py` → `engine/waterfall.py`, `profiling.py` → `quality/profiling.py`.
Scripts follow: `run_profile.py`, `run_waterfall.py`. The old `quick_win_gross_to_net.py`
now forwards to `run_waterfall.py` rather than keeping a second copy of the same
arithmetic alive.

## 3. The design decision that matters: one fact table

Everything is computed once at the finest available grain — the transaction
**line** — and every aggregate is a groupby over that one table. There is no
second calculation to keep in sync. A waterfall that is computed separately per
grain is a waterfall that eventually disagrees with itself.

Discounts hang off the *transaction*, not the line, so reaching line grain
requires allocating each transaction's discount across its lines pro-rata by
`line_total`. This is an assumption, and it is declared rather than buried:

- **Exact in aggregate.** Allocated amounts sum back to the transaction total to
  the cent — tested in `test_line_rollup_equals_transaction`. Transaction-grain
  and above are therefore unaffected by the choice.
- **An assumption at product grain.** A rebate triggered by one product but
  spread across the order will be mis-attributed. Pro-rata by value is the
  standard convention and the only defensible default while the graph gives us
  no line-level `DISCOUNTED_BY` edge.
- **One place to change.** If AXIOM later exposes that edge, `ALLOCATION_METHOD`
  marks the single line to replace.

Two aggregation rules are enforced, because both are common ways a pricing
dashboard ends up lying:

1. **Ratios come from sums, never means of ratios.** Segment margin % is
   `sum(margin)/sum(pocket)`. Averaging per-deal percentages weights a €400
   order the same as a €400,000 one.
2. **Coverage denominators come from the full population,** not the certified
   subset — otherwise every group reports 100% coverage by construction.

Every aggregation is checked against the grand total on every run
(`reconciles_with_total`). It costs milliseconds and catches the entire class of
join-fanout bugs that are invisible in the numbers themselves.

## 4. The result contract (D8 draft) — for Mohamed

Four design choices, each aimed at making the Brief's rule enforceable rather
than merely stated ("the narrative layer explains only numbers the engine
computed"):

| Choice | Why |
|---|---|
| `evidence` on every record | Track B cites *these* transaction/customer/product IDs and no others. A record that cannot name its sources fails validation. |
| `Money`, not `float` | You cannot read `.amount` without seeing `.status`. Missing is structurally distinguishable from zero at the type level. |
| `Confidence` carries `reasons` | The narrative is required to explain low confidence, so it needs the *why*, not just a number. |
| `Provenance` on the envelope | Snapshot hash, engine version, contract version, MLflow run ID. Any number regenerates. |

`contracts.validate()` runs before anything is emitted and rejects: records
claiming COMPLETE with no evidence, low confidence with no reason, steps that do
not reconcile, elasticities with a point estimate but no interval. It is tested
against a deliberately fabricated result to prove it actually rejects.

Sample payload: `reports/engine_result.json`. **Mohamed can build against this
now.** Additive changes only until the Week 7 freeze; anything breaking needs
both tracks to agree and a `CONTRACT_VERSION` bump.

## 5. Two findings that change how the numbers must be read

### 5.1 Five currencies, no FX rate — cross-currency aggregates now abstain

The graph carries a currency per region (AED, EUR, GBP, PLN, TND) and **no
conversion rate anywhere**. Any aggregate above region grain — including every
segment number — was therefore summing five currencies as if they were one unit.

This was a real defect in my own first cut of the aggregation, and it is exactly
what "abstain under uncertainty" is for. The engine now:

- reports `n_currencies` on every aggregated group;
- **refuses to publish** any group spanning more than one — amounts, ratios and
  confidence all suppressed, with the reason stated in the payload;
- raises it as a validation warning on every run, so it appears in the log.

The fix at the call site is to put currency in the grain:
`aggregate(w, ["segment", "currency"])`. That is now the pipeline default, and
it yields 20 publishable segment×currency records instead of 4 meaningless ones.

**→ Question for the gate:** are transaction amounts already normalised to a
single reporting currency, with `Region.currency` merely descriptive? If so this
is over-cautious and I will relax it. If not, we need an FX rate in the graph or
in the module's configuration, and the answer changes every aggregate we ship.

### 5.2 Enterprise is the least profitable segment

Reading within currency, the pattern is consistent across all five:

| Segment | Pocket margin % | Price realisation | Off-invoice leakage |
|---|---|---|---|
| Enterprise | 7.0% | 64.6% | 5.7% |
| Mid-Market | 24.2% | 77.1% | 3.7% |
| Public Sector | 28.8% | 83.2% | 1.7% |
| SMB | 32.2% | 87.4% | 2.0% |

*(cross-currency figures, shown for shape only — not published by the engine)*

The largest customers keep 7 cents of margin per euro of list while the smallest
keep 32. Enterprise also carries the highest off-invoice leakage, which is the
part nobody sees on an invoice. And **99 transactions carry negative pocket
margin — €410k of value sold below cost, 96% of it Enterprise.**

This is the module's reason for existing, visible in Week 2. It is also a
hypothesis to test, not a conclusion: the negative-margin deals may be
loss-leaders on service contracts, or they may be uncontrolled discounting. That
distinction is a question for the commercial team, and it is the kind of thing
Track B's narrative layer should be made to explain deal by deal.

## 6. MLOps

**MLflow** — every engine run logs parameters (snapshot hash, allocation method,
engine and contract version, grains, filters), metrics (all nine waterfall
steps, coverage, leakage ratios, flag counts, contract violations,
reconciliation failures) and artifacts (totals, every aggregation, the flags,
the serialised contract). No models yet, but a waterfall total is exactly the
kind of number a client later asks us to justify.

One change from the plan: MLflow 3.x has retired the `file:./mlruns` store, so
the local backend is now SQLite (`sqlite:///mlflow.db`). Still a single local
file, still air-gapped, and the model registry we need in Week 7 requires a
database backend anyway — so this migration was coming regardless.

Tracking failure never blocks a result: if the backend is unavailable the run
degrades to untracked and says so loudly.

**DVC** — the snapshot lives at `data/snapshot_synthetic_v1/` and is versioned
by content hash. `dvc.yaml` defines the `profile` and `waterfall` stages, so
`dvc repro` regenerates every report from versioned inputs with one command —
the acceptance criterion, working now rather than in Week 7.

MLflow and DVC meet at the snapshot hash: DVC versions the bytes, MLflow records
which bytes a result came from. Given a run you can find the exact data; given
the data you can regenerate the run.

## 7. Tests — 43, in three files

| File | What it defends |
|---|---|
| `test_graph.py` (10) | read-only Cypher (checked mechanically), referential integrity, semantic types, the validation gate accepting a degraded snapshot and rejecting a broken one, the `net_amount` observation |
| `test_engine.py` (20) | waterfall vs an independent hand computation at 0.01%, allocation losslessness, reconciliation across all six dimensions, value-weighting vs mean-of-ratios, coverage denominators, thin groups abstaining, missing-is-not-zero |
| `test_contract.py` (13) | evidence on every published record, low confidence explained, cross-currency abstention, JSON round-trip, and that the validator rejects a fabricated result |

Two bugs were caught by writing these rather than by reading the code: the
cross-currency sum in §5.1, and undiscounted full-price sales being read as
"discount amount unknown" and silently dropped from the certified book.

## 8. Open questions for the review

1. **Currency** (§5.1) — the highest-impact question. Are amounts pre-normalised,
   or do we need an FX rate?
2. **`net_amount` semantics** (carried from Week 1) — it nets *off*-invoice
   discounts, the reverse of convention. Intended, or a generator artefact? The
   engine currently ignores the field and computes from the discount edges.
3. **Channel `cost_to_serve` / `commission_rate`** — inside pocket margin, or
   below it? Currently excluded and reported separately. This changes every
   margin number, so it should be declared, not inferred.
4. **The negotiated off-list gap (11.9% of list)** — a real waterfall step, or
   loosely maintained list prices? No line is priced above list (0 of 1,408),
   which suggests list is a genuine ceiling and the gap is real discounting that
   happens before any `Discount` node is involved.
5. **Discount attribution** — is a line-level `DISCOUNTED_BY` edge feasible
   upstream? It would remove the pro-rata assumption at product grain.
6. **Three null product fields** still cost us 24% of the book. Worth asking the
   upstream team to complete them.

## 9. Into Week 3–4

D2 is substantially built, so Phase 1 shifts toward D3: margin analytics (IQR
outliers, leakage KPIs, top leakage points) and the 0–100 Pricing Data Readiness
Score that gates the advanced features. The confidence score in `result.py` is
deliberately a placeholder — coverage lightly penalised for thin groups — and
the readiness score supersedes it.

The negative-margin finding (§5.2) is the first thing I want to put in front of
someone commercial, because if those 99 deals are uncontrolled discounting
rather than deliberate loss-leaders, that alone justifies the module.
