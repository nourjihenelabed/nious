# Week 3 — Price Waterfall Engine (D2)

**AXIOM Pricing Module · Track A (Pricing Engine & MLOps)** · Nourjihen Elabed
Phase 1 · engine v0.3.0 · contract v0.2.0-draft
Snapshot `data/snapshot_synthetic_v1` · 700 transactions · 78 tests passing

---

## 1. What Week 3 asked for

> Implement the Price Waterfall Engine (D2): the full decomposition
> list → on-invoice → invoice → off-invoice → pocket → COGS → margin.
> Aggregation by customer, product, segment, channel, region and time period.
> Graceful missing-data handling: absent steps show as 'unavailable', never
> silently zero.
>
> **Output:** waterfall engine with unit tests against a hand-computed reference
> dataset.

| Required | Status |
|---|---|
| Full list → pocket margin decomposition | Done (built in Week 2, hardened here) |
| Aggregation by all six dimensions | Done, each one reconciled against the grand total on every run |
| Missing steps UNAVAILABLE, never zero | Done, and now **stated explicitly** per step rather than implied by a NaN |
| **Unit tests against a hand-computed reference dataset** | **Done — this was the gap Week 3 closed** |
| Matches a manual reference within 0.01% | **Exact: 279 comparisons, max difference 0.00** |

The decomposition itself was pulled forward into Week 2. What Week 3 added is
the thing that makes it *acceptable* rather than merely written: an independent
oracle, and the two defects that oracle found.

## 2. The reference dataset

`tests/reference/` holds a purpose-built fixture: two regions in two currencies,
two channels, four customers, five products, four discounts, eleven
transactions, fourteen lines. Every number is round. The expected answers were
worked out longhand in [`WEEK3_REFERENCE_ARITHMETIC.md`](WEEK3_REFERENCE_ARITHMETIC.md)
and committed as CSV before the engine was pointed at the fixture.

**Why this was worth doing.** `tests/test_engine.py` already checked the
vectorised engine against a naive loop-based re-implementation. That catches a
typo in one of them — but both read the same synthetic snapshot, so a
misunderstanding of what a column *means* passes twice and looks like
agreement. A hand-built fixture removes the shared assumption.

Each transaction pins exactly one behaviour:

| txn | pins |
|---|---|
| R01 | no discount edges → a true zero, still certified |
| R02 / R03 | on-invoice only / off-invoice only |
| R04 | two lines, both types → the pro-rata allocation, 80/40 and 48/24 |
| R05 | sold below cost → negative margin is a finding |
| R06 | no list price → list value UNAVAILABLE |
| R07 | no unit cost → COGS and margin UNAVAILABLE |
| R08 | discount of unknown amount → leakage UNAVAILABLE, steps above survive |
| R09 | one good line of two → the whole transaction uncertified |
| R10 | a GBP deal → the currency rule has something to bite on |
| R11 | free-of-charge order → a zero denominator must not become infinity |

The oracle also checks itself: `test_reference_expectations_are_internally_consistent`
sums every dimension back to the grand total, so a slip in my arithmetic fails
before any engine assertion runs. That distinguishes "the engine is broken" from
"I cannot add up" — which is exactly the distinction a reference dataset is
supposed to make cheap.

**Verification, three ways:**

```bash
pytest tests/test_reference.py -q      # 35 tests
python scripts/run_reference_check.py  # the reviewable side-by-side table
dvc repro reference_check              # the same, as a pipeline stage
```

The script writes `reports/reference_check.md`: every one of the 279
engine-vs-hand comparisons, per-scope summary, step availability. A reviewer can
check the claim without running Python.

## 3. Defect one — ratios with a zero denominator became ±infinity

R11 is a free-of-charge order: 300 of list value shipped for nothing, 160 of
cost incurred. Pocket price is a true zero, so:

```python
pocket_margin_pct = 100 * (-160) / 0        # -inf
```

A margin percentage with a zero denominator **does not exist**. It is not
infinite. And `-inf` is uniquely dangerous in this codebase, because it defeats
every guard built for the missing case:

- `pd.isna(-inf)` is `False`, so it flows through `_money` as a real number;
- it crosses the contract boundary into a `WaterfallRecord`;
- `json.dumps` writes it as `-Infinity`, which **no other JSON parser accepts** —
  Track B's payload would fail to load, and the failure would surface as a
  parse error on Mohamed's side rather than as a data-quality flag on mine.

The 700-transaction snapshot has no zero-pocket group, so nothing had ever
exercised the path.

**Fixed** by routing every ratio through `waterfall.ratio()` /
`waterfall.scalar_ratio()`, which return NaN when the denominator is zero or
missing — putting it back on the same "missing is not zero" rails as every other
absent number. Two further layers now make the class of bug non-recurring:

- `contracts.validate()` rejects any non-finite amount or percentage outright;
- `EngineResult.to_json()` passes `allow_nan=False`, so a payload that would be
  invalid JSON raises here rather than arriving broken at Track B.

## 4. Defect two — partial sums presented as results

R09 has two lines: one complete, one whose product has no unit cost. The
aggregator summed each step with `Series.sum(min_count=1)`, which returns a
number as long as *one* row had one. So the transaction's COGS came out as
600.00 — the cost of one line of two, presented as the cost of the order.

That is worse than a visible gap, because nothing about it looks wrong. A
600.00 COGS against a 1 325.00 pocket price reads as a healthy 55% margin on a
deal whose margin is unknown.

The engine's existing protection is certification: R09 is uncertified and is
excluded from every certified aggregate, which is the default path. But
`certified_only=False` is how an analyst inspects the *incomplete* part of the
book, and that is precisely where a partial sum is most misleading.

**Fixed** with `aggregation.complete_sum()`: a step is summed only if every
contributing row has it, otherwise UNAVAILABLE. Inside the certified scope every
value is present by construction, so the headline numbers are unchanged —
`reconciliation.csv` still passes on all six dimensions and every existing test
still holds. The same rule now applies in `totals_from`.

This is the "missing is not zero" rule extended one step further: *a partial sum
is not a result either.* Both defects came from the same reflex — reaching for
the pandas default rather than deciding what the absent case should mean.

## 5. Step availability — making "unavailable" visible

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

## 6. What the engine says about the real snapshot

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
part nobody sees on any document, and it is where the module earns its keep.

Reading within a single currency, the segment pattern from Week 2 holds and
sharpens:

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

This is a hypothesis, not a conclusion. Those deals may be deliberate
loss-leaders attached to service contracts, or they may be uncontrolled
discounting. The engine cannot tell the difference and should not pretend to —
that is a question for the commercial team, and the deal-by-deal explanation is
exactly what Track B's narrative layer is for.

## 7. Tests — 78, in four files

| File | Count | What it defends |
|---|---|---|
| `test_graph.py` | 10 | read-only Cypher (checked mechanically), referential integrity, semantic types, the validation gate accepting a degraded snapshot and rejecting a broken one |
| `test_engine.py` | 20 | waterfall vs an independent implementation, allocation losslessness, reconciliation across six dimensions, value-weighting vs mean-of-ratios, coverage denominators, thin groups abstaining |
| `test_contract.py` | 13 | evidence on every published record, low confidence explained, cross-currency abstention, JSON round-trip, the validator rejecting a fabricated result |
| **`test_reference.py`** | **35** | **the hand-computed oracle: every transaction, every dimension, both defects above, and the JSON payload** |

Every defect found so far in this codebase was found by writing a test that
encoded an expectation, never by re-reading the code. Week 2 found the
cross-currency sum and the undiscounted-sale misread; Week 3 found the
infinities and the partial sums. That is four for four, and it is the argument
for spending a week of Phase 1 on an oracle rather than on more features.

## 8. Reproducing all of it

```bash
dvc repro                              # reference_check -> profile -> waterfall
pytest -q                              # 78 tests
python scripts/run_reference_check.py  # D2 acceptance evidence
python -m mlflow ui --backend-store-uri sqlite:///mlflow.db
```

`reference_check` is now the **first** DVC stage: if the engine no longer agrees
with the hand arithmetic, the pipeline stops before it produces a report anyone
might act on.

## 9. Open questions carried into Week 4

Unchanged from the Week 2 gate, and all still blocking a *final* reading of the
numbers rather than the engine itself:

1. **Currency** — are transaction amounts pre-normalised to one reporting
   currency, with `Region.currency` merely descriptive? If yes, the abstention
   is over-cautious and I will relax it. If no, we need an FX rate. Highest
   impact question on the list; it changes every aggregate above region grain.
2. **`net_amount` semantics** — it nets *off*-invoice discounts, the reverse of
   convention. The engine ignores the field and computes from the discount
   edges. Intended, or a generator artefact?
3. **Channel `cost_to_serve` / `commission_rate`** — inside pocket margin or
   below it? Currently excluded and reported separately. Including them would
   move every margin number, so it should be declared, not inferred.
4. **The negotiated off-list gap (11.9% of list)** — a real waterfall step, or
   loosely maintained list prices? No line in 1 408 is priced above list, which
   suggests list is a genuine ceiling and the gap is real pre-`Discount`
   negotiation.
5. **Line-level `DISCOUNTED_BY`** — feasible upstream? It would remove the
   pro-rata assumption at product grain, which is the only modelling assumption
   in an otherwise fully deterministic deliverable.
6. **Three null product fields** cost 24% of the book its certification. Worth
   asking the upstream team to complete them before Week 5's segmentation
   inherits the gap.

## 10. Into Week 4 (D3)

The waterfall is done and defended. Week 4 is margin analytics and the readiness
score:

- margin distribution and IQR outlier detection over `waterfall_facts.csv`;
- leakage KPIs and top leakage points — `leakage_ranking()` is a first cut and
  needs a per-step attribution, not just a total;
- the **0–100 Pricing Data Readiness Score** that gates segmentation and
  optimisation. `availability()` is the natural input: per-step coverage,
  certification rate, and the flag counts already exist as data.

The confidence score in `result.py` remains a deliberate placeholder — coverage
lightly penalised for thin groups — and the readiness score supersedes it.

The negative-margin finding is still the first thing I want in front of someone
commercial. If those 99 deals are uncontrolled discounting rather than
deliberate loss-leaders, that alone justifies the module.
