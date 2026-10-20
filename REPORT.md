# AXIOM Pricing Module 
## Engineering report: what was built, why, and how to run it

**Nourjihen Elabed** · Pricing Engine & MLOps · NIOUS GmbH internship
Engine `v0.4.0` · contract `v0.2.0-draft` · 178 tests · Weeks 1–6 complete

---

## How to read this document

This is the standalone companion to the code. Everything the source files used
to explain in comments now lives here, so the code reads as code and the
reasoning reads as prose.

- **§1–2** — the assignment and what has been delivered against it.
- **§3** — architecture: the layers, why they are arranged this way, and the
  rules the whole codebase enforces.
- **§4** — every module, file by file, function by function.
- **§5** — Week 3 in detail: the reference dataset and the two defects it found.
- **§6** — what the numbers currently say about the data.
- **§7** — **how to run and test everything**, on Windows now and WSL later.
- **§8** — what I am expecting: gate criteria, open questions, what comes next.

The source files carry a one-line docstring per module and per function. If you
want to know *why* a function does what it does, it is in §4 here.

---

## 1. The assignment

NIOUS builds AXIOM, a platform that turns messy organisational data into a
governed, auditable knowledge graph. The Pricing Module is a new capability on
top of it: B2B pricing intelligence read out of that graph — where margin leaks,
how customers group by price behaviour, what to charge for a given deal, and
what a price change would do.

The module has two tracks that meet through one interface:

| Track | Owner | Owns |
|---|---|---|
| **A — Pricing Engine & MLOps** | **me** | the numbers: waterfall, segmentation, elasticity, win-probability, optimisation, and the MLOps that makes them reproducible |
| B — Hybrid GraphRAG & Narrative | Mohamed Dhya Shiri | retrieval and grounded, cited, bilingual explanation of those numbers |

The rule that keeps the whole thing honest, from the Brief:

> The narrative layer explains only numbers the engine computed. It never
> invents or recalculates figures.

So my structured output is the single source of truth, and it has to carry
enough with it — evidence, confidence, provenance — that Track B can explain it
without ever needing to recompute anything.

### My deliverables

| # | Deliverable | Week | Status |
|---|---|---|---|
| D1 | Graph-access layer | 2 | done |
| D2 | Price Waterfall Engine | 3 | **done — accepted against a hand-computed reference** |
| D3 | Margin analytics + Pricing Data Readiness Score | 4 | next |
| D4 | Customer segmentation | 5 | — |
| D5 | Elasticity module | 5 | — |
| D6 | Win-probability + constrained optimiser | 6 | — |
| D7 | MLOps packaging | 7 | partly done early (MLflow, DVC) |
| D8 | Result contract + docs | 7 | draft in use since Week 2 |

### The four governance principles I am building against

These are not decoration. Almost every design decision in §3 and §4 traces back
to one of them.

1. **Declared, not inferred.** Where a rule or definition should be fixed, fix
   it and document it. Prefer explicit checkable logic over opaque guesses.
2. **Grounded and cited.** Every result traces back to specific records in the
   graph. The engine records which records it used.
3. **Abstain under uncertainty.** Quantify confidence. When data quality is too
   low or evidence too thin, return low confidence or abstain — do not invent.
4. **Reproducible.** The same inputs must produce the same outputs. Version data
   and models so any result can be regenerated and audited.

---

## 2. What has been delivered

### Week 1 — environment and graph familiarisation

Set up Python, the Neo4j driver, MLflow, DVC and Docker; profiled the snapshot
(counts, distributions, null rates); wrote up the data as I understood it; and
built the quick win — a script that pulls one customer's transactions and
computes gross-to-net.

Output: working environment, `notebooks/01_graph_profiling.ipynb`,
`docs/WEEK1_DATA_WRITEUP.md`.

**The finding that mattered:** `net_amount` in this snapshot equals
`gross_amount` minus *off*-invoice discounts — the reverse of the textbook
meaning of "net". I did not guess which reading was intended. The engine ignores
the field entirely and computes from the `DISCOUNTED_BY` edges, and
`test_net_amount_semantics_are_as_observed` pins the observed behaviour so we
find out immediately if the real graph differs. That is principle 1 in practice:
when the data contradicts the convention, declare which you are using.

### Week 2 — architecture, D1, and the result schema

Designed the module structure, built the graph-access layer, drafted the
engine → narrative result schema with Mohamed, stood up MLflow tracking and DVC
versioning of the snapshot. D2 — the waterfall — was pulled forward from Week 3
because it is the most certain ground in the whole module and having real
aggregated numbers surfaced the data findings a week earlier.

Output: `docs/WEEK2_ARCHITECTURE.md`, `notebooks/02_waterfall_analysis.ipynb`.

**Two bugs found by writing tests, not by reading code:**

- Every aggregate above region grain was summing five currencies with no FX
  rate. The engine now refuses to publish any group spanning more than one.
- Undiscounted full-price sales were being read as "discount amount unknown" and
  silently dropped out of the certified book.

### Week 3 — D2 accepted

The decomposition already existed. What Week 3 added is what makes it
*acceptable* rather than merely written: a hand-computed reference dataset, the
tests that check the engine against it, and the fixes for the two defects that
exercise surfaced.

Output: `tests/reference/`, `tests/test_reference.py` (35 tests),
`scripts/run_reference_check.py`, `docs/WEEK3_WATERFALL.md`,
`docs/WEEK3_REFERENCE_ARITHMETIC.md`. Full detail in §5.

---

## 3. Architecture

### 3.1 The boundary I work inside

```
  Inside AXIOM                    The handoff                What I build
  (I never touch this)
  ─────────────────────      ──────────────────────      ──────────────────────
  ingestion                  a read-only Neo4j            the Pricing Module:
  schema discovery      →    knowledge graph of      →    an engine that reads
  quality enforcement        synthetic B2B data,          the graph and returns
  graph materialisation      plus the input and           structured, cited,
  the ontology engine        output contracts             confidence-bearing
  the source code                                         results
```

I have no access to AXIOM's source, which is normal and deliberate. What I have
is a schema contract for the input and an output contract I define myself. The
module is a well-behaved guest: it knows exactly what it receives and exactly
what it hands back.

### 3.2 Layers

```
src/pricing/
  contracts/   the engine -> narrative result schema (D8). Track B's only import.
  engine/      waterfall.py · aggregation.py · result.py · pipeline.py
  quality/     profiling.py                    (Week 4: the readiness score)
  graph/       source.py · cypher.py · validation.py     (D1)
  tracking/    mlflow_runs.py                  (Week 7: the model registry)
  config.py    backend selection, snapshot resolution, domain constants
```

**Dependencies point inward only.** `graph` knows nothing about `engine`;
`engine` knows nothing about `tracking`. `engine/pipeline.py` is the single
place they meet, and the single entry point every script, notebook and (later)
container calls.

The practical payoff: Week 5's segmentation and elasticity modules drop into
`engine/` without touching data access, and Week 7's containerisation wraps one
function.

### 3.3 The design decision that matters: one fact table

Everything is computed **once**, at the finest grain available — the transaction
**line** — and every aggregate (customer, product, segment, channel, region,
period) is a `groupby` over that one table.

The alternative, a separate calculation per grain, is how waterfalls end up
disagreeing with themselves. One fact table means there is no second calculation
to keep in sync.

**The complication:** discounts hang off the *transaction*, not the line. Until
Week 4 the engine bridged that by allocating each transaction's discounts across
its lines pro-rata by `line_total`, so that one line-grain fact table could serve
every grain. That allocation has been **removed**; the full argument is in
[`docs/WEEK4_GRAIN_AND_READINESS.md`](docs/WEEK4_GRAIN_AND_READINESS.md) §1, and
the short version is that pro-rata by value assumes the concession was *earned*
in proportion to line value, which nothing in the graph says and which is wrong
for a single-line concession, a volume tier and a relationship rebate alike.

So there are now two frames rather than one:

| frame | grain | carries |
|---|---|---|
| `w.facts` | one row per line | steps knowable per line: `list_value`, `negotiated_off_list`, `gross_value`, `cogs` |
| `w.keys` | one row per transaction | every step, with the line measures rolled **up** into it |

Rolling up is assumption-free; rolling down is not, and that asymmetry is the
whole design. `aggregate()` chooses its frame from the dimensions asked for: a
grouping whose columns are all constant within a transaction (customer, segment,
channel, region, period) reads `w.keys` and reports every step; a grouping that
splits a transaction (product, category, family) reads `w.facts`, and the
transaction-grain steps come back UNAVAILABLE with `grain_note` filled in.

The cost was confined to exactly one place. Grand totals and every
transaction-safe aggregate are **unchanged to the cent**, because allocation was
lossless; all 279 comparisons in the D2 reference still pass. What changed is
that product grain reports four steps instead of nine, and says why for the other
five.

If AXIOM later exposes a line-level `DISCOUNTED_BY` edge, one line changes: the
step's `grain` in the profile moves from `KEY` to `LINE`, and everything below it
follows through `propagate_grain`. No engine code is involved.

### 3.4 The rules this codebase enforces

Each of these is a rule the code refuses to break, not a convention I try to
remember. Each has at least one test whose only job is to fail if the rule is
broken.

#### Missing is not zero

A step whose inputs are unknown is UNAVAILABLE — NaN internally, `Money` with
`status=UNAVAILABLE` and a stated reason at the contract boundary. It never
becomes 0.

The reason this rule is absolute: zeroing a missing unit cost reports a 100%
margin, which is the most dangerous possible lie in a pricing report. Zeroing a
missing list price reports 100% price realisation on a deal nobody has a list
price for. Zeroing an unknown discount amount understates leakage — the exact
number the module exists to find.

Three states must stay distinguishable, and the code keeps them apart
deliberately:

| state | meaning | representation |
|---|---|---|
| no discount edge exists | genuinely no discount was given | **0.0**, certified |
| edges exist, amounts unknown | we gave something away, amount not recorded | **NaN**, uncertified |
| the field was never populated | upstream gap | **NaN**, uncertified, with a reason |

The line in `profiles._b2b_key_measures` that sets `amounts.loc[..., col] = 0.0`
where the transaction has no edge of that kind is what keeps an undiscounted
full-price sale in the certified book. Getting that wrong (as I did first) drops
profitable deals from the denominator and makes average leakage look worse than
it is.

#### A partial sum is not a result either

A step is summed only when **every** contributing row has it. Half an order's
COGS presented as the order's COGS is worse than a visible gap, because nothing
about it looks wrong. This is `aggregation.complete_sum`, and it is the Week 3
extension of the rule above (§5.4).

Consequently a transaction is certified only if **all** of its lines are: one
incomplete line poisons the whole order.

#### Ratios come from sums, never means of ratios

Pocket margin % for a segment is `sum(margin) / sum(pocket)`, never
`mean(margin % per deal)`. Averaging per-deal percentages weights a €400 order
the same as a €400,000 one, and is the single most common way a pricing
dashboard ends up lying. `test_aggregation_uses_sums_not_means_of_ratios` checks
both that the engine uses the right formula *and* that the two formulas actually
differ on this data — so the test cannot pass vacuously.

#### A ratio with a zero denominator does not exist

It is UNAVAILABLE, never ±infinity. See §5.3 for why this one bit.

#### Coverage denominators come from the full population

`coverage_pct = certified / ALL`, never `certified / certified` — the latter
makes every group report 100% coverage by construction, which is exactly the lie
the metric exists to prevent.

#### Aggregates must reconcile

Every `groupby` is checked against the grand total on every run
(`reconciles_with_total`). It costs milliseconds and catches the entire class of
join-fanout bugs that silently double-count and are invisible in the numbers
themselves.

#### No cross-currency sums

The graph carries a currency per region (AED, EUR, GBP, PLN, TND) and **no
conversion rate anywhere**. Any aggregate spanning currencies is a number with
no unit. The engine reports `n_currencies` on every group and refuses to publish
any group spanning more than one — amounts, ratios and confidence all suppressed,
with the reason stated in the payload. The fix at the call site is to put
currency in the grain: `aggregate(w, ["segment", "currency"])`, which is now the
pipeline default and yields 20 publishable segment×currency records instead of 4
meaningless ones.

#### Thin groups abstain rather than vanish

A group below the `min_transactions` threshold is returned with UNAVAILABLE
amounts and `status="abstained"`, not dropped. A segment that quietly disappears
from a report is worse than one that is visibly blank, because nobody notices
the absence.

#### Nothing is published without evidence

Every record in the result contract carries the transaction, customer and
product IDs it was computed from, plus a confidence score and the reasons behind
it. `contracts.validate()` rejects results that break this.

### 3.5 Data flow, end to end

```
  CSV snapshot  or  Neo4j
          │
          ▼
    GraphSource                      typed, read-only, backend-agnostic
          │
          ▼
    validation.require_valid()       refuse to run on a structurally broken snapshot
          │
          ▼
    waterfall.build_facts()          ONE line-grain fact table
          │
          ├──► totals_from()         the grand total, certified only
          ├──► flags_from()          data-quality findings with blast radius
          ├──► step_availability()   what each step can and cannot report
          │
          ▼
    aggregation.aggregate()          groupby to any dimension
          │
          ▼
    reconciles_with_total()          self-check: groups must sum to the total
          │
          ▼
    result.build()                   translate to typed records + evidence + confidence
          │
          ▼
    contracts.validate()             reject anything Track B could not defend
          │
          ├──► reports/*.csv, engine_result.json      (durable, resumable)
          └──► MLflow run                             (params, metrics, artefacts)
```

This is `engine/pipeline.py::run()`, and it is the only place the steps are
wired together. That is what keeps "how a result is produced" in one auditable
place — the practical version of the Brief's stage-gate requirement: *validate
inputs at the entry point and refuse to run on incomplete data; persist results
durably; log what was computed, when, and from which data snapshot.*

---

## 4. What the code does, module by module

### 4.1 `config.py` — one place for anything environment-dependent

Everything that could differ between "laptop with CSVs" and "NIOUS dev box with
Neo4j" lives here, so no other module needs to know which backend is active.
Read from environment variables; nothing here reaches the network at import
time.

| name | what it does |
|---|---|
| `REPO_ROOT`, `DATA_DIR`, `REPORTS_DIR`, `DOCS_DIR` | paths resolved from this file's location, not the working directory, so `python scripts/...` and `pytest` from anywhere agree |
| `_default_snapshot()` | picks the **newest** `data/snapshot_*` directory. Dated snapshots sort after `snapshot_synthetic_v1`, so a real export automatically takes precedence over the placeholder without any code change |
| `SOURCE` | `PRICING_SOURCE`: `csv` (current) or `neo4j` |
| `Neo4jSettings` | frozen dataclass of connection settings; `is_configured()` is just "has a password" |
| `ON_INVOICE` / `OFF_INVOICE` | the discount taxonomy from the Data Contract, imported rather than hardcoded in three places |
| `CSV_FILES` | the canonical filenames. The Neo4j → CSV exporter writes exactly these, so swapping backends never changes the rest of the code |

### 4.2 `graph/` — the read-only access layer (D1)

#### `graph/cypher.py`

Every Cypher query in one file. Two reasons it is separate: the queries are the
*real* interface to AXIOM, so the NIOUS team can review them without reading
Python; and the CSV exporter and the live reader use the exact same queries, so
the two backends cannot drift apart.

Every query is read-only by construction — no `CREATE`, `MERGE`, `SET`, `DELETE`
anywhere. That is not a promise, it is checked mechanically by
`test_no_write_clauses_in_cypher`, which reads the file and scans for write
clauses.

Node queries return a flat row shape matching the CSV column headers exactly.
Edges that carry pricing meaning (`CONTAINS.quantity/unit_price/line_total`,
`DISCOUNTED_BY.discount_amount_applied`) become their own tables.
`CUSTOMER_TRANSACTIONS` pushes the filter into Cypher rather than pulling the
whole graph, which matters once the graph is production-sized.

#### `graph/source.py`

The rest of the engine only ever talks to a `GraphSource`. Two implementations,
returning DataFrames with **identical** column names and dtypes:

- **`CsvGraphSource`** — active now. Reads a frozen CSV snapshot. Tables are
  cached after first read, because the snapshot is immutable within a run, and
  that is what makes a run reproducible. `snapshot_info()` returns a sha256 over
  the snapshot bytes — the link between a result and the exact data that
  produced it.
- **`Neo4jGraphSource`** — written, not yet exercised (no credentials as of
  Week 3). The driver session opens in `READ_ACCESS` mode, so even a coding
  mistake cannot write to the client graph. `neo4j` is imported lazily so the
  CSV path does not need the dependency.

`_normalise()` applies the semantic types from the Data Contract without
inventing values: `errors="coerce"` turns unparseable junk into NaN rather than
crashing, and the profiler then reports it as a data-quality finding.
`keep_default_na` behaviour means empty cells become NaN, **not** 0 or `""`.

`get_source()` honours `PRICING_SOURCE`. The day credentials arrive, the only
change is in `.env`.

#### `graph/validation.py`

The refuse-to-run gate. It runs once, before any pricing computation, and
answers one question: is this snapshot fit to compute on at all?

Two severities, deliberately separated:

- **BLOCKING** — the snapshot is structurally broken. Refuse to run. A missing
  table, a transaction with no customer, line items referencing products that do
  not exist, duplicate keys, negative money, a transaction with no lines, or a
  header `gross_amount` that disagrees with the sum of its line totals.
- **WARNING** — the snapshot is usable but degraded. Run, and carry the warnings
  into the result's confidence reasons. Products without a unit cost, discount
  edges without an amount, multiple currencies with no FX rate.

The distinction matters. Refusing to run on any imperfection would make the
module useless on real data; running silently on broken data would make it
dangerous. **The blocking set is small and is about structure, never about
completeness.**

Two judgement calls worth naming:

- *Negative money is blocking, not a warning.* It is a contradiction, not a gap.
- *Header/line disagreement is blocking.* If the transaction header and the
  `CONTAINS` edges disagree, we do not know which is true. That is structural.
- *Multiple currencies is only a warning.* Transaction-grain and region-grain
  results are still perfectly valid. It is only aggregates that **span**
  currencies that are meaningless, and the engine refuses those individually.
  Flagging it here means it appears in every run log.

`require_valid()` raises `SnapshotRejected` on any blocking finding. A loud,
specific failure is much better than a quiet wrong number.

### 4.3 `engine/waterfall.py` — the deterministic core (D2)

No modelling, no estimation. If a number here is wrong, it is wrong for a reason
we can point at. This is why it is the first and most certain deliverable.

```
List value            = Σ over lines of  quantity × Product.list_price
(−) negotiated off-list          the gap between list and what was invoiced
= Gross (invoiced) value = Σ over lines of  CONTAINS.line_total
(−) on-invoice discounts         DISCOUNTED_BY where Discount.type = on-invoice
= Invoice price
(−) off-invoice leakage          rebates, freight, terms, co-op, side letters
= Pocket price
(−) COGS              = Σ over lines of  quantity × Product.unit_cost
= Pocket margin
```

In plain terms: start from what the goods would have cost at list price, then
subtract every deduction, concession and cost in turn — the negotiated gap below
list, the discounts printed on the invoice, the off-invoice leakage that never
appears on any document, and finally the cost of the goods. What survives is the
pocket margin: what the seller actually keeps. The point of doing it step by
step is that **each step is a place where money left, and you can name it**.

| name | what it does |
|---|---|
| `STEPS` | the nine steps in order. Single source of truth: reports, plots, the contract and the reconciliation tests all read this tuple |
| `DEDUCTIONS` | which steps are subtractions rather than levels — used for plotting and for checking that levels reconcile |
| `DIMENSIONS` | the six grains D2 requires the waterfall to aggregate by |
| `GRAIN_POLICY` | names the policy for a measure known only at document grain: report it there, abstain below it. There is no alternative value — the setting exists to name the rule in one place, not to offer a choice of fictions |
| `PROFILE` | which source schema the engine is reading; see `pricing.profiles.REGISTRY` |
| `Waterfall` | the result: `facts` (line grain — the thing everything else derives from), plus `totals`, `flags`, `snapshot` provenance |
| `Waterfall.by_transaction()` | rolls up to one row per transaction. `certified_only=False` shows the incomplete part of the book |
| `ratio()` / `scalar_ratio()` | percentages with a guarded denominator — see §5.3 |
| `_dimension_columns()` | joins every slicing attribute (customer, segment, region, currency, channel) once, up front |
| `profiles._b2b_key_measures()` | the transaction-grain concessions, kept at transaction grain, plus the three-state discount logic from §3.4. This is where the pro-rata allocation used to be |
| `build_facts()` | the fact table: one row per (transaction, product) with every step, every dimension, and the certification flags |
| `totals_from()` | sums to a single waterfall. Certified rows only by default — mixing complete and incomplete rows makes the steps stop reconciling, which is worse than a smaller honest number |
| `step_availability()` | per step, how much of the book can report it. See §5.5 |
| `flags_from()` | the five data-quality findings, each with its blast radius in lines and transactions, and its consequence in words |
| `compute()` / `for_customer()` | entry points |
| `format_waterfall()` | renders the stepped waterfall with coverage stated above it |

**Certification.** A line is certified when every step is known. A transaction
is certified only if all of its lines are. `uncertified_reason` says why, in
words, and those words become `Confidence.reasons` on the contract — because an
exclusion nobody can explain is indistinguishable from a bug.

**A note on `flags_from`.** "Negative pocket margin" is listed as a flag but its
consequence reads *"selling below cost — a real finding, not an error"*. That is
deliberate. The module exists to surface exactly this. It is flagged so a reader
sees it, never suppressed or clipped.

### 4.4 `engine/aggregation.py` — roll it up

> *"Aggregatable by customer, product, segment, channel, region, period."* — D2

Because the engine keeps one line-grain fact table, aggregation is a `groupby`
and nothing more.

| name | what it does |
|---|---|
| `DIMENSION_ALIASES` | friendly names, so callers write `aggregate(w, "segment")` without knowing column names |
| `LABEL_FOR` | descriptive labels carried alongside IDs, so an aggregate is readable without a second join |
| `resolve()` | alias → column |
| `complete_sum()` | sum a step, or UNAVAILABLE if any contributing row is missing it (§5.4) |
| `aggregate()` | the main function: sums, currency counts, coverage against the full population, ratios from sums, thin-group abstention, readable labels |
| `by_all_dimensions()` | every required D2 aggregation in one call |
| `leakage_ranking()` | where margin leaks most — ranked by **absolute leaked value**, not percentage, because a 40% leak on a small account is interesting but a 12% leak on the largest account is a budget |
| `reconciles_with_total()` | the self-check |
| `waterfall_bridge()` | one aggregated row → a plottable bridge, keeping chart code out of notebooks |

What aggregation must **not** do, restated as the four things `aggregate()`
actively prevents: average percentages; mix certified and uncertified rows into
one total; drop empty groups silently; sum across currencies.

### 4.5 `contracts/schema.py` — the Track A / Track B boundary (D8)

This file is the interface I own. Everything in it is designed to make the
Brief's rule *enforceable* rather than merely stated.

| type | why it exists |
|---|---|
| `ResultStatus` | COMPLETE / PARTIAL / UNAVAILABLE / ABSTAINED. A field that is `None` because data was missing is never confusable with a field that is legitimately zero |
| `Money` | an amount that can be absent **for a stated reason**. Using this instead of a bare `float` is the type-level version of "missing is not zero": you cannot read `.amount` without also seeing `.status`. `__bool__` is overridden so `if money:` cannot silently mean `if amount:` |
| `Confidence` | a *value*, not an adjective: score, band, and the **reasons**. The narrative is required to explain low confidence, so it needs the why, not just a number |
| `Evidence` | the transaction / customer / product / discount IDs a number was computed from. A result that cannot name its sources is not shippable |
| `Provenance` | snapshot backend, location and hash, engine version, contract version, MLflow run ID. Without this a result is not auditable |
| `WaterfallRecord` | one waterfall at whatever grain. `grain` and `key` say what the row *is*, so a narrative never has to infer it from context |
| `SegmentAssignment`, `ElasticityEstimate`, `PriceRecommendation`, `PriceRange` | placeholders for D4/D5/D6, defined now so Mohamed can build against the full shape early. `PriceRange` rejects an unordered band at construction |
| `EngineResult` | the envelope. Deliberately a container of **typed records** rather than a free-form dict: Track B should write `result.waterfalls[0].pocket_price.amount` and get a guarantee, not fish through JSON keys |

`to_json()` passes `allow_nan=False`. Python would otherwise happily write `NaN`
and `-Infinity`, which no other JSON parser accepts. Better to fail here, loudly,
than to hand Track B a payload it cannot load.

`validate()` runs before anything is emitted and rejects: non-finite amounts or
percentages; records claiming COMPLETE with no evidence; COMPLETE with zero
confidence; low confidence with no reason; steps that do not reconcile;
elasticities with a point estimate but no credible interval; recommendations
that are COMPLETE with no price range or no comparable deals. It is cheap, and
it catches the class of bug that would otherwise surface as a confidently-worded
wrong narrative. `test_contract_validate_catches_a_fabricated_result` feeds it a
deliberately broken result to prove it actually rejects.

**Status: draft.** Agreed in principle with Mohamed in Week 2, to be **frozen in
Week 7**. Additive changes only until then; any breaking change needs both
tracks to sign off and a bump of `CONTRACT_VERSION`. A sample payload lives at
`reports/engine_result.json` — Mohamed can build against it now.

### 4.6 `engine/result.py` — the translation layer

The only module allowed to build `EngineResult` objects. The separation is
deliberate: internally the engine speaks pandas, because that is what the maths
wants; externally it speaks typed records with evidence and confidence attached,
because that is what a governance product needs. Keeping the translation in one
file means the contract can be frozen in Week 7 without freezing how the
waterfall is computed.

| name | what it does |
|---|---|
| `_finite()` | coerce to a finite float or `None`. NaN and ±inf are both "no number" |
| `_money()` / `_pct()` | number → `Money` / rounded float; anything not finite → explicit UNAVAILABLE |
| `_confidence_for()` | **deliberately simple**: confidence *is* coverage, lightly penalised when the group is thin. One certified deal cannot be "high confidence" however complete its data is. A clever opaque score would be worse here — Track B has to justify this number to a reader. The real Pricing Data Readiness Score (D3, Week 4) supersedes it |
| `_status_for()` | a cross-currency sum is not a number we can defend: ABSTAINED, with the fix stated |
| `_evidence_for()` | the certified records behind a row, capped at 200 IDs. Track B needs enough to cite, not the entire book; the full set is always recoverable by re-running the same groupby, and the cap keeps a payload usable at real data volumes |
| `waterfall_records()` | aggregate → typed records. On a currency mix, amounts *and ratios* are suppressed: a ratio survives a currency mix only if every leg shares a unit, which is exactly what "mixed" means it does not |
| `build()` | assembles the envelope with provenance and period |

### 4.7 `engine/pipeline.py` — the single entry point

`run()` is `validate → waterfall → aggregate → reconcile → contract → track`.
Scripts, notebooks and the future container all call it; none of them wire the
steps themselves.

`contract_grains` is kept narrower than `grains` on purpose: the contract is a
handover payload, not a data dump, and every record in it carries evidence IDs.
The default is `["segment", "currency"]` — a plain `"segment"` grain spans
currencies and the engine would refuse to publish it, so splitting by currency
makes every record a defensible number in a stated unit.

`PipelineRun.save()` writes every artefact durably — facts, totals, flags,
availability, each aggregation, the reconciliation, the validation report and
the serialised contract — so a run can be resumed, reviewed and audited.
`PipelineRun.ok` is true only when the contract validates *and* every aggregate
reconciles.

### 4.8 `quality/profiling.py` — describe, do not judge

Week 1 asks for "counts, distributions, null rates". This module produces those
as **data** (DataFrames), not as printed text, so the notebook, the CLI script
and later the readiness score all read the same numbers. Nothing here judges the
data; it reports. Interpretation lives in the Week 1 write-up.

`null_rates()` is the single most important Week 1 artefact: every null is a
decision the waterfall will have to make later (flag as unavailable vs. impute
vs. exclude), and the acceptance criteria forbid silently zeroing them.

`amount_reconciliation()` tests four hypotheses about what `gross_amount` and
`net_amount` actually mean in this snapshot:

- H1 `gross == Σ line_total`
- H2 `net == gross − on-invoice` (the textbook reading)
- H3 `net == gross − all discounts`
- H4 `net == gross − off-invoice`

H1 and **H4** hold; H2 does not. Whichever holds tells us where `net_amount`
sits on the waterfall. Since the answer is the non-conventional one, we do not
guess — we ask, and meanwhile compute from the discount edges.

### 4.9 `tracking/mlflow_runs.py` — experiment tracking

There are no trained models yet, so what gets tracked is the **deterministic**
engine run — and that is not a placeholder. The reproducibility requirement is
that any result can be regenerated from versioned data plus a recorded run, and
a waterfall total is exactly the kind of number a client will later ask us to
justify.

Each run logs:

- **params** — snapshot hash, backend, allocation method, engine and contract
  version, grains computed, filters applied
- **metrics** — every waterfall step, coverage, leakage ratios, per-step
  availability, flag counts, contract violations, reconciliation failures
- **artefacts** — totals, every aggregation, the flags, the availability table,
  and the serialised result contract

**The snapshot hash is the link between MLflow and DVC:** DVC versions the
bytes, MLflow records which bytes a result came from. Given a run you can find
the exact data; given the data you can regenerate the run.

**Backend: local SQLite** (`sqlite:///mlflow.db`) — a single file next to the
repo, no server, no external calls, matching the sovereign air-gapped deployment
target. This is a change from the plan's `file:./mlruns` store, for two reasons:
MLflow 3.x put the file store in maintenance mode and refuses to use it without
an opt-out flag, and the model registry we need in Week 7 requires a database
backend anyway — so this migration was coming regardless.

**MLflow is an optional dependency.** If it is not installed, or the backend is
unavailable, `run_context` yields a no-op and says so loudly. Tracking must
never be the reason a result cannot be produced. (Common cause of an unavailable
backend: `mlflow-skinny` is installed, which omits the SQLAlchemy store. Install
full `mlflow`.)

### 4.10 `scripts/`

| script | what it does |
|---|---|
| `run_reference_check.py` | **the D2 gate artefact.** Engine vs the hand-computed reference, 279 comparisons, written to `reports/reference_check.md` as a table a reviewer can read without running Python. Exit code 0 only if every number agrees |
| `run_waterfall.py` | the whole book or one customer; optional single-dimension print and leakage ranking; writes everything to `reports/` and logs to MLflow |
| `run_profile.py` | the full data profile → `reports/profile_*.csv` and `profile_report.md`. Works against whichever backend `PRICING_SOURCE` selects, so the same command will profile the real graph |
| `export_neo4j_to_csv.py` | freezes the live graph into the canonical CSV snapshot. Read-only, writes the same filenames and headers, and `na_rep=""` so NULL stays NULL and is never coerced to 0. Always writes a `_manifest.json`, because a snapshot without provenance is not auditable. `--verify` cross-checks exported row counts against graph node/relationship counts |
| `quick_win_gross_to_net.py` | deprecated Week 1 script, now forwards to `run_waterfall.py --customer` rather than keeping a second copy of the same arithmetic alive |

### 4.11 `dvc.yaml` — reproducibility as one command

Three stages, in order: `reference_check` → `profile` → `waterfall`.

`reference_check` is deliberately **first**: if the engine no longer agrees with
the hand arithmetic, the pipeline stops before producing a report anyone might
act on.

The snapshot itself is versioned separately with `dvc add`, because it is an
input we receive rather than something this pipeline produces.

---

## 5. Week 3 in detail

### 5.1 What Week 3 asked for

> Implement the Price Waterfall Engine (D2)… Aggregation by customer, product,
> segment, channel, region and time period. Graceful missing-data handling:
> absent steps show as 'unavailable', never silently zero.
> **Output: waterfall engine with unit tests against a hand-computed reference
> dataset.**

The decomposition and the aggregation were built in Week 2. The gap was the
oracle.

### 5.2 The reference dataset, and why a second one was needed

`tests/test_engine.py` already checked the vectorised engine against a naive
loop-based re-implementation. That catches a typo in either version — but both
read the same 700-transaction synthetic snapshot, so if I have misunderstood
what a column *means*, both implementations agree and both are wrong. Agreement
between two things that share an assumption is not evidence.

`tests/reference/` is a purpose-built fixture with the shared assumption
removed: two regions in two currencies, two channels, four customers, five
products, four discounts, eleven transactions, fourteen lines. Every number is
round. The expected answers were worked out longhand in
`docs/WEEK3_REFERENCE_ARITHMETIC.md` and committed as CSV **before** the engine
was pointed at the fixture.

Each transaction pins exactly one behaviour — no discount edges (a true zero),
on-invoice only, off-invoice only, two lines with both types (the allocation),
sold below cost, missing list price, missing unit cost, a discount of unknown
amount, one good line of two, a GBP deal, and a free-of-charge order.

The oracle also checks itself:
`test_reference_expectations_are_internally_consistent` sums every dimension
back to the grand total, so a slip in my arithmetic fails *before* any engine
assertion runs. That distinguishes "the engine is broken" from "I cannot add
up" — which is exactly the distinction a reference dataset is supposed to make
cheap.

**Result: 279 comparisons, maximum absolute difference 0.00.** Exact, not merely
within the 0.01% the criterion allows.

### 5.3 Defect one — ratios with a zero denominator became ±infinity

Transaction R11 in the fixture is a free-of-charge order: 300 of list value
shipped for nothing, 160 of cost incurred. Pocket price is a true zero, so:

```python
pocket_margin_pct = 100 * (-160) / 0        # -inf
```

A margin percentage with a zero denominator **does not exist**. It is not
infinite. And `-inf` is uniquely dangerous here because it defeats every guard
built for the missing case:

- `pd.isna(-inf)` is `False`, so it flows through `_money` as a real number;
- it crosses the contract boundary into a `WaterfallRecord`;
- `json.dumps` writes it as `-Infinity`, which **no other JSON parser accepts** —
  Track B's payload would fail to load, and the failure would surface as a parse
  error on Mohamed's side rather than as a data-quality flag on mine.

The 700-transaction snapshot has no zero-pocket group, so nothing had ever
exercised the path.

**Fixed** by routing every ratio through `waterfall.ratio()` /
`scalar_ratio()`, which return NaN when the denominator is zero or missing —
putting it back on the same "missing is not zero" rails as every other absent
number. Two further layers make the class of bug non-recurring:
`contracts.validate()` rejects any non-finite amount or percentage outright, and
`to_json(allow_nan=False)` raises rather than emitting invalid JSON.

### 5.4 Defect two — partial sums presented as results

Transaction R09 has two lines: one complete, one whose product has no unit cost.
The aggregator summed each step with `Series.sum(min_count=1)`, which returns a
number as long as *one* row had one. So the transaction's COGS came out as
600.00 — the cost of one line of two, presented as the cost of the order.

That is worse than a visible gap, because nothing about it looks wrong. A 600.00
COGS against a 1 325.00 pocket price reads as a healthy 55% margin on a deal
whose margin is unknown.

The existing protection is certification: R09 is uncertified and excluded from
every certified aggregate, which is the default path. But `certified_only=False`
is how an analyst inspects the *incomplete* part of the book, and that is
precisely where a partial sum is most misleading.

**Fixed** with `aggregation.complete_sum()`: a step is summed only if every
contributing row has it, otherwise UNAVAILABLE. Inside the certified scope every
value is present by construction, so the headline numbers are unchanged and every
existing test still holds. The same rule now applies in `totals_from`.

Both defects came from the same reflex — reaching for the pandas default rather
than deciding what the absent case should *mean*.

### 5.5 Step availability — making "unavailable" visible

D2 requires that absent steps show as unavailable. The fact table already
encoded that as NaN, but a NaN is easy to miss in a wide CSV, and a total that
is quietly short one transaction's leakage looks exactly like a total that is
complete.

`Waterfall.availability()` states it per step. On the real snapshot:

| step | lines available | lines missing | transactions able to report it | availability |
|---|---|---|---|---|
| list value | 1 348 | 60 | 640 / 700 | 95.74% |
| negotiated off-list | 1 348 | 60 | 640 / 700 | 95.74% |
| gross value | 1 408 | 0 | 700 / 700 | 100.00% |
| on-invoice discount | 1 408 | 0 | 700 / 700 | 100.00% |
| invoice price | 1 408 | 0 | 700 / 700 | 100.00% |
| off-invoice leakage | 1 405 | 3 | 698 / 700 | 99.79% |
| pocket price | 1 405 | 3 | 698 / 700 | 99.79% |
| COGS | 1 292 | 116 | 587 / 700 | 91.76% |
| pocket margin | 1 291 | 117 | 587 / 700 | 91.69% |

A transaction counts as able to report a step only when **every** one of its
lines can — the same rule as `complete_sum`, for the same reason.

Read down the table and the shape of the data problem is obvious: the invoice is
always fully known, and everything uncertain lives at the two ends — what the
goods should have listed at, and what they cost. Two null product fields cost
16% of the book its margin number.

The table is written to `reports/waterfall_availability.csv`, printed by
`run_waterfall.py`, and logged to MLflow as `available_pct__<step>` so a drop
between snapshots shows up in the run comparison rather than only in a report
nobody reopens.

---

## 6. What the numbers currently say

Whole book, certified transactions only (530 of 700, **75.7% coverage**):

```
List value                   31,108,764.18   (100.0% of list)
- negotiated off-list         3,688,235.18   ( 11.9%)
= Gross (invoiced) value     27,420,529.00   ( 88.1%)
- on-invoice discounts        3,669,609.59   ( 11.8%)
= Invoice price              23,750,919.41   ( 76.3%)
- off-invoice leakage         1,117,996.07   (  3.6%)
= Pocket price               22,632,923.34   ( 72.8%)
- COGS                       18,513,092.48   ( 59.5%)
= Pocket margin               4,119,830.86   ( 13.2%)

Pocket margin %                      18.20%
Price realisation                    72.75%
```

**27 cents of every list euro never reaches the pocket**, before cost. Roughly
12 points go before an invoice is even raised, another 12 on the invoice, and
3.6 off it. That last band is the classic finding — off-invoice leakage is the
part nobody sees on any document.

Reading within a single currency (the only publishable form), the segment
pattern is consistent across all five currencies:

| segment | pocket margin % | price realisation | off-invoice leakage % |
|---|---|---|---|
| Enterprise (EUR) | 2.84% | 63.04% | 5.90% |
| Enterprise (GBP) | 10.22% | 65.56% | 4.39% |
| Mid-Market (EUR) | 24.22% | 76.00% | 4.81% |
| Public Sector (EUR) | 31.44% | 84.74% | 1.69% |
| SMB (EUR) | 29.76% | 86.42% | 1.24% |

The largest accounts keep the least, and lose the most off-invoice. **99
transactions carry a negative pocket margin — €410k sold below cost, 96% of it
Enterprise.** The single worst account, CUS-029, gives away 37.4% of list and
retains 4.3% margin on €2.33m of list value.

**This is a hypothesis, not a conclusion.** Those deals may be deliberate
loss-leaders attached to service contracts, or they may be uncontrolled
discounting. The engine cannot tell the difference and should not pretend to.
That is a question for the commercial team, and the deal-by-deal explanation is
exactly what Track B's narrative layer is for.

---

## 7. How to run and test it

### 7.1 Environment

**Now: local Windows.** The virtual environment is `axiompricingvenv`, inside
the repo (and gitignored).

```bash
cd C:\Users\joujou\Desktop\nious\data\axiom-pricing-module
python -m venv axiompricingvenv
axiompricingvenv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**Later: WSL.** Nothing in the codebase is Windows-specific — every path is
built with `pathlib` and resolved from the repo root rather than the working
directory, and there is no shell-out anywhere. Moving over is:

```bash
cd ~/axiom-pricing-module
rm -rf axiompricingvenv                  # a venv is not portable across OSes
python3 -m venv axiompricingvenv
source axiompricingvenv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest -q                                # expect 78 passed
```

The only two things to be aware of when you move:

1. **Rebuild the venv.** `axiompricingvenv/Scripts/` (Windows) becomes
   `axiompricingvenv/bin/` (Linux); the binaries themselves do not transfer.
2. **`mlflow.db` and `mlartifacts/` carry absolute paths** in their artifact
   locations. Existing run *metrics* still read fine, but artifact links from
   Windows runs will point at `C:\...`. If that matters, delete `mlflow.db` and
   `mlartifacts/` and re-run — every result regenerates from versioned data by
   design. If it does not matter, ignore it; the numbers are unaffected.

**Always activate the venv before running anything.** `dvc repro` shells out to
whatever `python` is on `PATH`, so an unactivated shell will silently use the
system interpreter and fail on missing dependencies.

### 7.2 The one command that proves D2

```bash
python scripts/run_reference_check.py
```

Expected: `PASS: all 279 comparisons within 0.01%`, and
`reports/reference_check.md` written. Exit code 0.

This is the acceptance evidence for the Week 3 gate. It prints the grand total,
the step-availability table, a per-scope summary, and — in the markdown file —
every individual engine-vs-hand comparison.

### 7.3 The test suite

```bash
pytest -q                              # 178 tests, ~40 seconds (add -m slow for the MCMC fits)
pytest tests/test_reference.py -v      # the hand-computed oracle, 35 tests
pytest -k currency -v                  # everything about the FX problem
pytest -k "missing or zero" -v         # everything about missing-is-not-zero
```

| file | tests | what it defends |
|---|---|---|
| `test_graph.py` | 10 | read-only Cypher (checked mechanically), referential integrity, semantic types, the validation gate accepting a degraded snapshot and rejecting a broken one, the `net_amount` observation |
| `test_engine.py` | 20 | waterfall vs an independent implementation, allocation losslessness, reconciliation across six dimensions, value-weighting vs mean-of-ratios, coverage denominators, thin groups abstaining |
| `test_contract.py` | 13 | evidence on every published record, low confidence explained, cross-currency abstention, JSON round-trip, the validator rejecting a fabricated result |
| `test_reference.py` | 35 | the hand-computed oracle: every transaction, every dimension, both Week 3 defects, and the JSON payload |

**How to read a failure.** Every assertion carries the engine value and the
reference value in its message, e.g.
`TRX-R04.pocket_price: engine=1008.0 reference=1007.5`. For a reference-dataset
failure, open `docs/WEEK3_REFERENCE_ARITHMETIC.md` and find the transaction —
the longhand arithmetic is there, so you can check by eye which side is wrong.

### 7.4 Running the engine

```bash
python scripts/run_waterfall.py                     # whole book, all dimensions
python scripts/run_waterfall.py --customer CUS-013  # one customer
python scripts/run_waterfall.py --by segment        # print one aggregation
python scripts/run_waterfall.py --leakage customer  # where is margin leaking?
python scripts/run_waterfall.py --no-track          # skip MLflow
python scripts/run_profile.py                       # data profile -> reports/
```

From Python, everything goes through one entry point:

```python
from pricing.engine import pipeline

run = pipeline.run()          # validate -> waterfall -> aggregate -> contract -> track
print(run.waterfall.summary())
print(run.waterfall.availability())
run.result.to_json()          # exactly what Track B receives
run.ok                        # True only if contract valid AND aggregates reconcile
```

### 7.5 Reproducibility

```bash
dvc repro                                        # reference_check -> profile -> waterfall
dvc dag                                          # show the stage graph
mlflow ui --backend-store-uri sqlite:///mlflow.db   # then open http://localhost:5000
```

`dvc repro` regenerates every report from versioned inputs with one command.
That is the acceptance criterion — *"any result regenerates from versioned data
via a single pipeline command"* — working now rather than in Week 7.

### 7.6 What to check at review

A short checklist for anyone reviewing this:

1. `pytest -q` → **78 passed**.
2. `python scripts/run_reference_check.py` → **PASS, 279 comparisons, max diff 0.00**.
3. `dvc repro` → all three stages run, `dvc.lock` updated.
4. `reports/engine_result.json` → valid JSON, 20 records, no `NaN` or `Infinity`.
5. `reports/reconciliation.csv` → every row `ok=True`.
6. `reports/waterfall_availability.csv` → the per-step coverage table.
7. `reports/validation.txt` → 0 blocking, 4 warnings (all expected and named).

### 7.7 Switching to the real Neo4j graph

Nothing in the engine changes. When credentials arrive:

```bash
# option A - read the live graph directly
PRICING_SOURCE=neo4j        # + NEO4J_* in .env

# option B (preferred, and what reproducibility needs) - freeze a snapshot
python scripts/export_neo4j_to_csv.py --out data/snapshot_2026-07-08 --verify
dvc add data/snapshot_2026-07-08
```

The exporter writes the same filenames and column headers, and `config.py`
automatically picks the newest `data/snapshot_*`, so every notebook, script and
test keeps working unchanged — now on real data.

Option B is preferred because a run must be regenerable from a *frozen* snapshot,
not from a live database that may have moved underneath us.

---

## 8. What I am expecting

### 8.1 Against the Week 3 gate

> *Gate — Demo + code review: waterfall accuracy verified (<0.01% variance);
> KPIs match an independent calculation.*

| criterion | evidence |
|---|---|
| Decomposition matches a manual reference within 0.01% | **exact** — 279 comparisons, max diff 0.00, `reports/reference_check.md` |
| All steps populated where data exists; missing steps flagged, not zeroed | `step_availability()`, `flags`, `uncertified_reason`, and 9 tests specifically on this |
| Aggregates correctly by customer, product, segment, channel, region, arbitrary periods | all six hand-checked, all six reconciled with the grand total on every run |
| KPIs match an independent calculation | two independent checks: a loop-based re-implementation over the real snapshot, and the hand-computed oracle |

### 8.2 Against the shared definition of done

| | |
|---|---|
| **Correct** | verified against a hand-computed reference within tolerance — in fact exactly |
| **Grounded** | every published record carries the transaction / customer / product IDs it was computed from; `validate()` rejects records that cite nothing |
| **Honest about uncertainty** | confidence is a value with reasons; cross-currency and thin groups abstain; missing steps are UNAVAILABLE with a stated cause |
| **Reproducible** | `dvc repro` regenerates everything; every run is logged to MLflow keyed to the snapshot hash |
| **Reviewed and documented** | this report, seven gate documents, one-line docstrings throughout, 178 tests |

### 8.3 Open questions for the review

All six are still open from the Week 2 gate. None blocks the engine; all block a
*final* reading of the numbers.

1. **Currency — highest impact.** Are transaction amounts already normalised to
   a single reporting currency, with `Region.currency` merely descriptive? If
   yes, the abstention is over-cautious and I will relax it. If no, we need an
   FX rate in the graph or in the module's configuration. The answer changes
   every aggregate above region grain.
2. **`net_amount` semantics.** It nets *off*-invoice discounts, the reverse of
   convention. Intended, or a generator artefact? The engine currently ignores
   the field.
3. **Channel `cost_to_serve` / `commission_rate`.** Inside pocket margin, or
   below it? Currently excluded and reported separately. This moves every margin
   number, so it should be declared, not inferred.
4. **The negotiated off-list gap (11.9% of list).** A real waterfall step, or
   loosely maintained list prices? No line in 1 408 is priced above list, which
   suggests list is a genuine ceiling and the gap is real discounting that
   happens before any `Discount` node is involved.
5. **Line-level `DISCOUNTED_BY`.** Feasible upstream? Since Week 4 the engine no
   longer allocates, so this is no longer a question about an assumption — it is
   a question about *coverage*. Without that edge, product, category and family
   grain cannot report any concession step at all. This is now the single
   highest-value upstream ask.
6. **Three null product fields** cost 24% of the book its certification. Worth
   asking the upstream team to complete them; Week 5's segmentation inherits the
   gap and abstains where it bites.

### 8.4 Weeks 4–6 — what was built

**Week 4 (D3).** Margin analytics (`analytics/margin.py`: leakage bridge, price
bands, margin quadrants, margin at risk) and the 0–100 **Pricing Data Readiness
Score** (`quality/readiness.py`) — five weighted checks plus a per-deliverable
READY / LIMITED / BLOCKED verdict that names the missing field. The same week
removed the pro-rata allocation and made the waterfall chain a declaration rather
than engine code, which is what let a second, completely different schema be
onboarded without touching `engine/`. Write-up:
[`docs/WEEK4_GRAIN_AND_READINESS.md`](docs/WEEK4_GRAIN_AND_READINESS.md).

**Week 5 (D4, D5).** Behavioural segmentation on eight relative features, with
economic cluster names and abstention for thin customers; elasticity as a
shrunk log-log slope that is never reported without a credible interval, and is
gated out of optimisation unless the interval stays negative. Write-up:
[`docs/WEEK5_SEGMENTATION_AND_ELASTICITY.md`](docs/WEEK5_SEGMENTATION_AND_ELASTICITY.md).

**Week 6 (D6).** The graph records no lost quotes, so no win rate is estimable
from it; `models/acceptance.py` estimates the concession benchmark instead and
labels it as such everywhere it surfaces, including refusing to score its own
calibration. `models/optimiser.py` maximises expected profit inside a feasible
region built from the margin floor, list price and a band around comparable
prices, and returns a band rather than a point. Write-up:
[`docs/WEEK6_ACCEPTANCE_AND_OPTIMISATION.md`](docs/WEEK6_ACCEPTANCE_AND_OPTIMISATION.md).

The confidence score in `result.py` remains a deliberate placeholder — coverage
lightly penalised for thin groups — and the readiness score supersedes it for
deciding what may run at all.

### 8.5 Week 7–8 — packaging and the contract freeze

- freeze `CONTRACT_VERSION` at `0.2.0` and stop calling it a draft;
- register the fitted segmentation and elasticity models in MLflow, keyed to the
  snapshot hash they were fitted on;
- put `run_readiness.py` in front of the DVC pipeline, so a snapshot that cannot
  support a deliverable stops the run rather than producing one;
- integrate with Track B against the frozen contract.

### 8.6 A note on how the defects have been found

Every defect found in this codebase so far was found by writing a test that
encoded an expectation, never by re-reading the code:

| week | defect | found by |
|---|---|---|
| 2 | cross-currency sums with no FX rate | writing the aggregation test |
| 2 | undiscounted sales read as "amount unknown" | writing the true-zero test |
| 3 | ±infinity from a zero denominator, breaking JSON | the free-of-charge fixture transaction |
| 3 | partial sums presented as complete results | the one-good-line-of-two fixture transaction |
| 5 | elasticities near -10, from dividing price by the quantity being regressed | plotting the fitted slopes and refusing to believe them |
| 5 | segment records shipping a LOW confidence band with no reason | running `contracts.validate()` over the model output |
| 6 | an unordered price band | `PriceRange.__post_init__` rejecting it at construction |
| 6 | a swapped-in CSV silently under-reporting | writing the conformance check against a deliberately damaged snapshot |
| 6 | the PyTensor backend flag set one line too late, costing 33x | timing the same fit twice and refusing to accept the difference |

Nine for nine, and the last two are the same lesson as the first four: the defect
was found by writing something that encoded an expectation, not by re-reading the
code that broke it. That is the argument for having spent a week of Phase 1 building
an oracle rather than more features, and it is why `reference_check` is the
first stage of the DVC pipeline rather than the last.

---

## Appendix — file inventory

```
axiom-pricing-module/
├── REPORT.md                          this document
├── README.md                          quick start and progress
├── requirements.txt                   pinned loosely; freeze exact versions at the Phase 1 gate
├── dvc.yaml / dvc.lock                reference_check -> profile -> waterfall
├── docker-compose.yml                 optional local Neo4j + MLflow UI
├── .env.example                       every environment variable, documented
│
├── docs/
│   ├── WEEK1_DATA_WRITEUP.md          the data as I understood it in Week 1
│   ├── WEEK2_ARCHITECTURE.md          Phase 0 gate: architecture review
│   ├── WEEK3_WATERFALL.md             Phase 1 gate: D2 accepted
│   └── WEEK3_REFERENCE_ARITHMETIC.md  the manual reference, worked out longhand
│
├── src/pricing/
│   ├── config.py                      backend selection, snapshot resolution, constants
│   ├── contracts/schema.py            D8: the Track A / Track B boundary
│   ├── engine/
│   │   ├── waterfall.py               D2: the deterministic core
│   │   ├── aggregation.py             D2: roll up to any dimension
│   │   ├── result.py                  pandas -> typed contract records
│   │   └── pipeline.py                the single entry point
│   ├── graph/
│   │   ├── source.py                  D1: CsvGraphSource | Neo4jGraphSource
│   │   ├── cypher.py                  every query, read-only by construction
│   │   └── validation.py              the refuse-to-run gate
│   ├── quality/profiling.py           counts, nulls, distributions, reconciliation
│   └── tracking/mlflow_runs.py        experiment tracking, SQLite backend
│
├── scripts/
│   ├── run_reference_check.py         D2 acceptance evidence
│   ├── run_waterfall.py               run the engine
│   ├── run_profile.py                 profile the snapshot
│   ├── export_neo4j_to_csv.py         freeze the live graph
│   └── quick_win_gross_to_net.py      deprecated, forwards
│
├── tests/
│   ├── test_graph.py                  10 tests
│   ├── test_engine.py                 20 tests
│   ├── test_contract.py               13 tests
│   ├── test_reference.py              35 tests
│   └── reference/
│       ├── snapshot/                  the eleven-transaction fixture (8 CSVs)
│       ├── expected_transactions.csv  the hand-computed answer, per transaction
│       └── expected_aggregates.csv    the hand-computed answer, per dimension
│
├── notebooks/
│   ├── 01_graph_profiling.ipynb       Week 1 exploration
│   └── 02_waterfall_analysis.ipynb    Week 2 analysis and charts
│
├── data/snapshot_synthetic_v1/        DVC-versioned (not in git)
└── reports/                           generated; regenerate with dvc repro
```
