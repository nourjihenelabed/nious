# The reference dataset — arithmetic worked out by hand

**AXIOM Pricing Module · Track A · D2 acceptance evidence**

> *"Decomposition matches a manual reference within 0.01%."* — acceptance criteria, Waterfall correctness
> *"Waterfall engine with unit tests against a hand-computed reference dataset."* — Week 3 output

This document **is** the manual reference. Every number below was computed by
hand from the fixture in `tests/reference/snapshot/`, before the engine was
pointed at it. The answers are committed as
`tests/reference/expected_transactions.csv` and
`tests/reference/expected_aggregates.csv`; `tests/test_reference.py` asserts the
engine reproduces them, and `scripts/run_reference_check.py` prints the
comparison.

## Why a second reference at all

`tests/test_engine.py` already checks the vectorised engine against a naive
loop-based re-implementation. That catches a typo in either version, but both
read the same 700-transaction synthetic snapshot — so if I have misunderstood
what a column *means*, both implementations agree and both are wrong.

A hand-built fixture removes the shared assumption. The numbers are round, the
book is eleven transactions long, and the expected answers exist on paper before
any code runs. If the engine and the paper disagree, exactly one of them is
wrong and it is cheap to find out which.

The oracle is also checked against itself: `test_reference_expectations_are_internally_consistent`
sums every dimension back to the grand total. If my arithmetic slips, that fails
before a single engine assertion runs — which distinguishes "the engine is
broken" from "I can't add up".

## The fixture

Two regions (EUR and GBP, so the currency rule has something to bite on), two
channels, four customers across two segments, five products, four discounts,
eleven transactions, fourteen lines.

### Products

| id | list price | unit cost | why it exists |
|---|---|---|---|
| PRD-1 | 100.00 | 60.00 | ordinary |
| PRD-2 | 200.00 | 150.00 | thin margin — used for the below-cost case |
| PRD-3 | 50.00 | 20.00 | ordinary |
| PRD-4 | *(none)* | 40.00 | list value must become UNAVAILABLE |
| PRD-5 | 80.00 | *(none)* | COGS and margin must become UNAVAILABLE |

### Customers

| id | segment | region | currency |
|---|---|---|---|
| CUS-A1 | Enterprise | REG-A | EUR |
| CUS-A2 | SMB | REG-A | EUR |
| CUS-A3 | SMB | REG-A | EUR |
| CUS-B1 | Enterprise | REG-B | GBP |

### What each transaction pins down

| txn | property under test |
|---|---|
| TRX-R01 | no discount edges at all → a **true zero**, still certified |
| TRX-R02 | on-invoice discount only |
| TRX-R03 | off-invoice discount only |
| TRX-R04 | two lines, both discount types → the **pro-rata allocation** |
| TRX-R05 | sold below cost → **negative margin is a finding, not an error** |
| TRX-R06 | product with no list price → list value UNAVAILABLE |
| TRX-R07 | product with no unit cost → COGS and margin UNAVAILABLE |
| TRX-R08 | discount edge with no amount → leakage UNAVAILABLE, steps above it survive |
| TRX-R09 | one good line, one bad → **the whole transaction is uncertified** |
| TRX-R10 | a GBP deal → the currency dimension is non-trivial |
| TRX-R11 | free-of-charge order → **a zero denominator must not become infinity** |

---

## Transaction by transaction

The waterfall, in the order the engine computes it:

```
list value          = Σ over lines of  quantity × Product.list_price
− negotiated off-list                  (the gap between list and what was invoiced)
= gross value       = Σ over lines of  CONTAINS.line_total
− on-invoice discounts                 (DISCOUNTED_BY where Discount.type = on-invoice)
= invoice price
− off-invoice leakage                  (rebates, freight, terms, co-op, side letters)
= pocket price
− COGS              = Σ over lines of  quantity × Product.unit_cost
= pocket margin
```

### TRX-R01 — full price, no concessions · CUS-A1, CH-D, 2026-01-15

One line: PRD-1 × 10 at 90.00 → line total 900.00. No `DISCOUNTED_BY` edges.

```
list value            10 × 100.00  =  1 000.00
− negotiated off-list  1000 − 900  =    100.00
= gross value                          900.00
− on-invoice                              0.00   ← no edge of this type: a zero we know
= invoice price                        900.00
− off-invoice                             0.00   ← likewise
= pocket price                         900.00
− COGS                 10 ×  60.00  =    600.00
= pocket margin                        300.00
```

**Certified.** The zeros here are the point: a transaction with no discount edge
genuinely had no discount. That is different from "edges exist, amounts unknown"
(TRX-R08) and different again from "we never looked". Reading it as unknown
would drop a full-price sale out of the certified book and quietly overstate
average leakage.

### TRX-R02 — on-invoice only · CUS-A1, CH-D, 2026-01-20

PRD-2 × 5 at 180.00 → 900.00. `DSC-ON-A` applied: 90.00.

```
list value             5 × 200.00  =  1 000.00
− negotiated off-list                   100.00
= gross value                           900.00
− on-invoice                             90.00
= invoice price                         810.00
− off-invoice                             0.00
= pocket price                          810.00
− COGS                 5 × 150.00  =    750.00
= pocket margin                          60.00
```

**Certified.**

### TRX-R03 — off-invoice only · CUS-A2, CH-P, 2026-02-10

PRD-3 × 20 at 40.00 → 800.00. `DSC-OFF-A` applied: 32.00.

```
list value            20 ×  50.00  =  1 000.00
− negotiated off-list                   200.00
= gross value                           800.00
− on-invoice                              0.00
= invoice price                         800.00
− off-invoice                            32.00
= pocket price                          768.00
− COGS                20 ×  20.00  =    400.00
= pocket margin                         368.00
```

**Certified.**

### TRX-R04 — two lines, both discount types · CUS-A2, CH-D, 2026-02-15

| line | product | qty | unit price | line total |
|---|---|---|---|---|
| a | PRD-1 | 10 | 80.00 | 800.00 |
| b | PRD-3 | 10 | 40.00 | 400.00 |

Discounts: `DSC-ON-A` 120.00 (on-invoice), `DSC-OFF-A` 48.00 + `DSC-OFF-B` 24.00
= 72.00 (off-invoice).

Discounts hang off the **transaction**, not the line, so reaching line grain
means allocating them. The engine allocates pro-rata by `line_total`:

```
share(a) = 800 / 1200 = 2/3        share(b) = 400 / 1200 = 1/3

on-invoice   120.00 × 2/3 =  80.00   |   120.00 × 1/3 =  40.00
off-invoice   72.00 × 2/3 =  48.00   |    72.00 × 1/3 =  24.00
```

Per line:

| step | line a (PRD-1) | line b (PRD-3) | transaction |
|---|---|---|---|
| list value | 10 × 100 = 1 000.00 | 10 × 50 = 500.00 | 1 500.00 |
| − negotiated off-list | 200.00 | 100.00 | 300.00 |
| = gross value | 800.00 | 400.00 | 1 200.00 |
| − on-invoice | 80.00 | 40.00 | 120.00 |
| = invoice price | 720.00 | 360.00 | 1 080.00 |
| − off-invoice | 48.00 | 24.00 | 72.00 |
| = pocket price | 672.00 | 336.00 | 1 008.00 |
| − COGS | 10 × 60 = 600.00 | 10 × 20 = 200.00 | 800.00 |
| = pocket margin | 72.00 | 136.00 | 208.00 |

**Certified.** The allocation is exact in aggregate — 80 + 40 = 120 and
48 + 24 = 72 — which is what lets the entire engine live on one line-grain fact
table. At *product* grain it is an assumption: a rebate triggered by PRD-1 but
spread across the order is mis-attributed to PRD-3. Pro-rata by value is the
standard convention and the only defensible default while the graph exposes no
line-level `DISCOUNTED_BY` edge. `ALLOCATION_METHOD` marks the one line to
change if it ever does.

### TRX-R05 — sold below cost · CUS-A1, CH-P, 2026-03-05

PRD-2 × 10 at 160.00 → 1 600.00. `DSC-ON-B` 80.00, `DSC-OFF-A` 64.00.

```
list value            10 × 200.00  =  2 000.00
− negotiated off-list                   400.00
= gross value                         1 600.00
− on-invoice                             80.00
= invoice price                       1 520.00
− off-invoice                            64.00
= pocket price                        1 456.00
− COGS                10 × 150.00  =  1 500.00
= pocket margin                       −  44.00
```

**Certified, and negative.** A negative pocket margin is a *result*, not an
error — it is the finding the module exists to surface. It is flagged in
`waterfall.flags`, never suppressed or clipped to zero.

### TRX-R06 — no list price · CUS-A3, CH-D, 2026-03-10

PRD-4 × 10 at 45.00 → 450.00. PRD-4 has no `list_price`.

```
list value                            UNAVAILABLE   ← not 0
− negotiated off-list                 UNAVAILABLE   ← depends on list value
= gross value                            450.00
− on-invoice                               0.00
= invoice price                          450.00
− off-invoice                              0.00
= pocket price                           450.00
− COGS                10 × 40.00  =      400.00
= pocket margin                           50.00
```

**Not certified** — reason: *product has no list_price*.

Two things matter here. First, the missing step is UNAVAILABLE rather than zero:
zeroing it would report 100% price realisation on a deal whose list price nobody
knows. Second, the steps that do **not** depend on the gap still compute — the
engine reports what it can and is silent only about what it cannot.

### TRX-R07 — no unit cost · CUS-A3, CH-D, 2026-03-12

PRD-5 × 10 at 70.00 → 700.00. PRD-5 has no `unit_cost`.

```
list value            10 × 80.00  =      800.00
− negotiated off-list                    100.00
= gross value                            700.00
− on-invoice                               0.00
= invoice price                          700.00
− off-invoice                              0.00
= pocket price                           700.00
− COGS                                UNAVAILABLE   ← not 0
= pocket margin                       UNAVAILABLE   ← depends on COGS
```

**Not certified** — reason: *product has no unit_cost*. Zeroing COGS would
report a 100% margin, the most dangerous possible lie in a pricing report.

### TRX-R08 — a discount of unknown amount · CUS-A1, CH-D, 2026-03-20

PRD-1 × 10 at 85.00 → 850.00. `DSC-ON-A` applied: 85.00. `DSC-OFF-B` applied:
**amount not recorded**.

```
list value            10 × 100.00  =   1 000.00
− negotiated off-list                    150.00
= gross value                            850.00
− on-invoice                              85.00   ← known, and still reported
= invoice price                          765.00
− off-invoice                         UNAVAILABLE   ← the edge exists, the amount does not
= pocket price                        UNAVAILABLE
− COGS                10 × 60.00  =      600.00
= pocket margin                       UNAVAILABLE
```

**Not certified** — reason: *discount applied with unknown amount*.

"We gave something away but do not know how much" cannot be zero: treating it as
zero understates leakage, which is precisely the number the module is asked to
find. Note that the *on-invoice* step is unaffected — an unknown of one type does
not destroy the other.

### TRX-R09 — one good line, one bad · CUS-B1, CH-D, 2026-04-01

| line | product | qty | unit price | line total |
|---|---|---|---|---|
| a | PRD-1 | 10 | 95.00 | 950.00 |
| b | PRD-5 | 5 | 75.00 | 375.00 |

Line a is complete. Line b uses PRD-5, which has no unit cost.

```
list value       (10 × 100) + (5 × 80)  =  1 400.00
− negotiated off-list                          75.00
= gross value          950 + 375        =  1 325.00
− on-invoice                                    0.00
= invoice price                            1 325.00
− off-invoice                                   0.00
= pocket price                             1 325.00
− COGS            600 + UNAVAILABLE     = UNAVAILABLE   ← not 600
= pocket margin                           UNAVAILABLE
```

**Not certified,** for the whole transaction, even though line a is perfect.

This is the case that exposed a real defect. COGS here is *not* 600: 600 is the
cost of one line of two, and presenting it as the transaction's COGS is a
partial sum wearing the clothes of a result. It is worse than a visible gap
because nothing about it looks wrong. `Series.sum(min_count=1)` — which returns
a number as long as one row had one — produced exactly that. The aggregator now
uses `complete_sum`, which returns UNAVAILABLE if *any* contributing row is
missing. See `docs/WEEK3_WATERFALL.md` §4.

### TRX-R10 — a GBP deal · CUS-B1, CH-P, 2026-04-10

PRD-3 × 40 at 45.00 → 1 800.00. `DSC-ON-A` 180.00, `DSC-OFF-B` 36.00.

```
list value            40 × 50.00  =    2 000.00
− negotiated off-list                    200.00
= gross value                          1 800.00
− on-invoice                             180.00
= invoice price                        1 620.00
− off-invoice                             36.00
= pocket price                         1 584.00
− COGS                40 × 20.00  =      800.00
= pocket margin                          784.00
```

**Certified,** and denominated in **GBP** — every other certified deal is EUR.
Any aggregate that spans this transaction and a EUR one (segment `Enterprise`,
for instance) is summing two currencies with no FX rate in the graph, and the
engine refuses to publish it.

### TRX-R11 — free of charge · CUS-A3, CH-D, 2026-04-15

Two lines shipped at zero: PRD-1 × 2 and PRD-3 × 2, both 0.00.

```
list value       (2 × 100) + (2 × 50)  =    300.00
− negotiated off-list                       300.00
= gross value                                 0.00
− on-invoice                                  0.00
= invoice price                               0.00
− off-invoice                                 0.00
= pocket price                                0.00
− COGS            (2 × 60) + (2 × 20)  =    160.00
= pocket margin                          −  160.00

pocket margin %   = −160 / 0           →  DOES NOT EXIST   ← not −∞
price realisation =    0 / 300         =  0.00%            ← a real zero
```

**Certified** — every input is known. This is the transaction that caught the
`±inf` defect: plain division gave `-inf`, which survives `pd.isna`, crosses the
contract boundary, and serialises as `-Infinity`, which is not valid JSON. A
percentage with a zero denominator does not exist; it is not infinite.

The zero-value order also exercises the allocation guard: pro-rata by value is
undefined when the order is worth nothing, so the engine falls back to an equal
split. With no discounts to allocate the amounts are zero either way, but the
division never happens.

---

## Rolling up

### Grand total — certified transactions only (7 of 11)

| step | R01 | R02 | R03 | R04 | R05 | R10 | R11 | **total** |
|---|---|---|---|---|---|---|---|---|
| list value | 1 000 | 1 000 | 1 000 | 1 500 | 2 000 | 2 000 | 300 | **8 800** |
| − negotiated off-list | 100 | 100 | 200 | 300 | 400 | 200 | 300 | **1 600** |
| = gross value | 900 | 900 | 800 | 1 200 | 1 600 | 1 800 | 0 | **7 200** |
| − on-invoice | 0 | 90 | 0 | 120 | 80 | 180 | 0 | **470** |
| = invoice price | 900 | 810 | 800 | 1 080 | 1 520 | 1 620 | 0 | **6 730** |
| − off-invoice | 0 | 0 | 32 | 72 | 64 | 36 | 0 | **204** |
| = pocket price | 900 | 810 | 768 | 1 008 | 1 456 | 1 584 | 0 | **6 526** |
| − COGS | 600 | 750 | 400 | 800 | 1 500 | 800 | 160 | **5 010** |
| = pocket margin | 300 | 60 | 368 | 208 | −44 | 784 | −160 | **1 516** |

The levels reconcile: 8 800 − 1 600 = 7 200 · 7 200 − 470 = 6 730 ·
6 730 − 204 = 6 526 · 6 526 − 5 010 = 1 516.

```
coverage           = 7 / 11                = 63.64%
pocket margin %    = 1 516 / 6 526         = 23.23%
price realisation  = 6 526 / 8 800         = 74.16%
```

Both ratios are **sums divided by sums**, never the mean of per-deal
percentages. The distinction is not academic: the mean of the seven per-deal
margin percentages weights the 300-of-list free order the same as the
2 000-of-list GBP deal.

### Every dimension, hand-computed

Each block below must sum back to the grand total. That is not decoration — a
groupby that fans out a join double-counts silently, and this is the cheapest
way to catch it.

**By customer**

| customer | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CUS-A1 | 4 000 | 600 | 3 400 | 170 | 3 230 | 64 | 3 166 | 2 850 | 316 | 4 | 3 |
| CUS-A2 | 2 500 | 500 | 2 000 | 120 | 1 880 | 104 | 1 776 | 1 200 | 576 | 2 | 2 |
| CUS-A3 | 300 | 300 | 0 | 0 | 0 | 0 | 0 | 160 | −160 | 3 | 1 |
| CUS-B1 | 2 000 | 200 | 1 800 | 180 | 1 620 | 36 | 1 584 | 800 | 784 | 2 | 1 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | **11** | **7** |

Note CUS-A3: three transactions, one certified — **33.33% coverage**, not 100%.
The denominator is the full population, not the certified subset. Getting that
backwards makes every group report perfect coverage by construction.

**By segment**

| segment | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Enterprise | 6 000 | 800 | 5 200 | 350 | 4 850 | 100 | 4 750 | 3 650 | 1 100 | 6 | 4 |
| SMB | 2 800 | 800 | 2 000 | 120 | 1 880 | 104 | 1 776 | 1 360 | 416 | 5 | 3 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | **11** | **7** |

Enterprise spans EUR (CUS-A1) and GBP (CUS-B1). The arithmetic above is what a
naive sum produces; the engine **refuses to publish it** and abstains with the
reason stated, because there is no FX rate in the graph. Splitting the grain —
`aggregate(w, ["segment", "currency"])` — makes each row a real number in a
stated unit.

**By channel**

| channel | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CH-D | 3 800 | 800 | 3 000 | 210 | 2 790 | 72 | 2 718 | 2 310 | 408 | 8 | 4 |
| CH-P | 5 000 | 800 | 4 200 | 260 | 3 940 | 132 | 3 808 | 2 700 | 1 108 | 3 | 3 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | **11** | **7** |

**By region**

| region | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| REG-A (EUR) | 6 800 | 1 400 | 5 400 | 290 | 5 110 | 168 | 4 942 | 4 210 | 732 | 9 | 6 |
| REG-B (GBP) | 2 000 | 200 | 1 800 | 180 | 1 620 | 36 | 1 584 | 800 | 784 | 2 | 1 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | **11** | **7** |

Region is the coarsest grain at which every row has a single currency, so these
two numbers are publishable as they stand.

**By period (month)**

| period | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01 | 2 000 | 200 | 1 800 | 90 | 1 710 | 0 | 1 710 | 1 350 | 360 | 2 | 2 |
| 2026-02 | 2 500 | 500 | 2 000 | 120 | 1 880 | 104 | 1 776 | 1 200 | 576 | 2 | 2 |
| 2026-03 | 2 000 | 400 | 1 600 | 80 | 1 520 | 64 | 1 456 | 1 500 | −44 | 4 | 1 |
| 2026-04 | 2 300 | 500 | 1 800 | 180 | 1 620 | 36 | 1 584 | 960 | 624 | 3 | 2 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | **11** | **7** |

March is the interesting month: four transactions, one certified. Three of the
four deliberate data defects land there, so its 25% coverage is the fixture's
worked example of "the number is small because the data is thin, and we say so".

**By product** — line grain, so a two-line transaction contributes to two
products and the transaction counts sum to 14 (the number of lines), not 11.

| product | list | negot. | gross | on-inv | invoice | off-inv | pocket | COGS | margin | txns | certified |
|---|---|---|---|---|---|---|---|---|---|---|---|
| PRD-1 | 2 200 | 500 | 1 700 | 80 | 1 620 | 48 | 1 572 | 1 320 | 252 | 5 | 3 |
| PRD-2 | 3 000 | 500 | 2 500 | 170 | 2 330 | 64 | 2 266 | 2 250 | 16 | 2 | 2 |
| PRD-3 | 3 600 | 600 | 3 000 | 220 | 2 780 | 92 | 2 688 | 1 440 | 1 248 | 4 | 4 |
| PRD-4 | — | — | — | — | — | — | — | — | — | 1 | 0 |
| PRD-5 | — | — | — | — | — | — | — | — | — | 2 | 0 |
| **Σ** | **8 800** | **1 600** | **7 200** | **470** | **6 730** | **204** | **6 526** | **5 010** | **1 516** | | **—** |

PRD-4 and PRD-5 sell, but nothing about them can be decomposed. They appear with
status `unavailable` rather than being dropped: a segment that quietly
disappears from a report is worse than one that is visibly blank, because
nobody notices the absence.

### Step availability

What the fourteen lines can and cannot report:

| step | lines available | lines missing | transactions able to report it |
|---|---|---|---|
| list value | 13 | 1 (R06) | 10 of 11 |
| negotiated off-list | 13 | 1 | 10 of 11 |
| gross value | 14 | 0 | 11 of 11 |
| on-invoice discount | 14 | 0 | 11 of 11 |
| invoice price | 14 | 0 | 11 of 11 |
| off-invoice leakage | 13 | 1 (R08) | 10 of 11 |
| pocket price | 13 | 1 | 10 of 11 |
| COGS | 12 | 2 (R07, R09b) | **9 of 11** |
| pocket margin | 11 | 3 (+ R08) | **8 of 11** |

A transaction counts as able to report a step only when **every** one of its
lines can — which is why R09 cannot report COGS despite one of its two lines
having a cost. Gaps also propagate downward: R08 loses its pocket margin not
because its cost is missing but because its pocket price is.

---

## Running the check

```bash
pytest tests/test_reference.py -q      # 27 assertions over these numbers
python scripts/run_reference_check.py  # the reviewable side-by-side table
dvc repro reference_check              # the same, as a pipeline stage
```

Current result: **279 comparisons, maximum absolute difference 0.00** — exact,
not merely within the 0.01% the criterion allows.
