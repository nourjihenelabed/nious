# Week 4 (D3) — grain, the end of pro-rata, and the readiness score

Owner: Nourjihen Elabed · engine v0.4.0 · 178 tests

Two things happened this week. One is the Week 4 deliverable on the plan: margin
analytics and the Pricing Data Readiness Score. The other is a change to the Week
3 engine that I would rather make now than defend for another five weeks: the
waterfall no longer allocates.

---

## 1. The pro-rata assumption, and why it is gone

### What it was

The graph records concessions against the **order**, not the line.
`DISCOUNTED_BY` hangs off the transaction. But the fact table was at line grain,
because a pricing module has to be able to say something about a product.

To bridge those two facts, the engine spread each order's discount across its
lines in proportion to line value:

```
TRX-R04:  PRD-1 line = 800,  PRD-3 line = 400,  on-invoice discount = 120
          -> PRD-1 gets 80,  PRD-3 gets 40
```

I documented it as the module's one modelling assumption and moved on. That was
the right call in Week 3 — the alternative was shipping nothing at product grain
— but it was wrong, and it was wrong in a way that is hard to see.

### Why it is wrong

Pro-rata by value assumes the concession was *earned* in proportion to line
value. Nothing in the graph says that. The three cases it gets wrong are all
common:

- **A single-line concession.** A customer negotiates on the one item they
  actually care about. Pro-rata smears it across the whole order, understating
  the discount on the contested product and overstating it everywhere else.
- **A volume-tier discount.** Earned by the order's total, not by any line. There
  is no line-level truth to recover, so any split is fiction.
- **A relationship rebate.** Earned by the customer over a year. Attributing it
  to a product is a category error.

The failure mode is not that the number is a bit off. It is that
`waterfall_by_product.csv` looked exactly as authoritative as
`waterfall_by_customer.csv`, and only one of them was.

### What replaced it

Nothing. That is the point.

Every measure now declares the finest grain at which it is **known**
(`pricing/spec.py`). The engine propagates that down the chain and keeps two
frames instead of one:

| frame | grain | carries |
|---|---|---|
| `w.facts` | one row per line | steps knowable per line: list value, negotiated off-list, gross value, COGS |
| `w.keys` | one row per order | every step, with line measures rolled **up** into it |

Rolling up is assumption-free; rolling down is not. That asymmetry is the whole
design. `aggregate()` then picks its frame from the dimensions asked for:

```python
aggregate(w, "customer")   # constant within an order -> w.keys, every step
aggregate(w, "product")    # varies within an order   -> w.facts, discounts abstain
```

A suppressed step comes back as UNAVAILABLE with `grain_note` filled in, not as a
plausible number.

### What it cost, and what it did not

Nothing at the top. **The grand total and every customer, segment, channel,
region and period aggregate are unchanged to the cent** — allocation was lossless,
so summing the parts always gave back the whole. All 279 comparisons in the
hand-computed D2 reference still pass.

Exactly one thing changed: the product grain no longer reports the concession
steps.

| grain | before | after |
|---|---|---|
| customer, segment, channel, region, period | 9 steps | 9 steps, identical values |
| product, category, family | 9 steps, 5 of them allocated | 4 steps + 5 stated UNAVAILABLE |

`tests/reference/expected_aggregates.csv` now leaves those cells blank for the
product dimension, and `test_reference_expectations_are_internally_consistent`
asserts they *must* be blank. The oracle encodes the grain rule rather than
working around it.

### If a line-level discount ever appears

One line changes: the step's `grain` in the profile moves from `KEY` to `LINE`,
and everything below it follows automatically through `propagate_grain`. No
engine code is involved.

---

## 2. Declared waterfalls

The chain used to be a nine-string tuple inside the engine. It is now a
`spec.Profile`: an ordered chain of `Step`s, each a level or a deduction, each
read from the data (`MEASURE`) or derived from its neighbours (`DERIVED`).

```python
Step("gross_value",         Kind.LEVEL,     Origin.MEASURE, LINE)
Step("on_invoice_discount", Kind.DEDUCTION, Origin.MEASURE, KEY)
Step("invoice_price",       Kind.LEVEL,     Origin.DERIVED)      # grain inferred: KEY
```

The chain is validated at construction, so a malformed profile fails at import
rather than producing a plausible wrong number. Adding a concession type — a
distributor allowance, a consignment cost — is adding a `Step` and one line of
extraction. It is not an engine change.

Two profiles ship: `b2b_industrial` (the synthetic distributor) and
`dairy_al_kharj` (below).

---

## 3. The Pricing Data Readiness Score (D3)

Point the module at a dataset and it says whether pricing analytics is possible
on it, and if not, which field would fix it. Five checks, each readable alone:

| check | weight | asks |
|---|---|---|
| structure | 0.20 | do the profile's tables exist and have rows? |
| chain | 0.30 | is every declared step actually measurable? |
| grain | 0.20 | how finely can the answer be sliced before it abstains? |
| integrity | 0.15 | did the entry gate find anything blocking? |
| depth | 0.15 | is there enough history for the model deliverables? |

On the synthetic snapshot:

```
Pricing Data Readiness: 83.3/100 -- READY (b2b_industrial)

  structure    100.0  8/8 tables readable, 4,188 rows in total
  chain         97.4  5 measures declared, mean coverage 97%
  grain         44.4  5/9 steps are document-grain
  integrity     68.0  no blocking findings, 4 warning(s)
  depth        100.0  700 documents; supports every deliverable
```

The grain check scoring 44 is the pro-rata finding, now expressed as a number
rather than a paragraph.

Underneath is the part that earns its keep. `capabilities()` answers per
deliverable, and when the answer is not READY it names what is missing:

```
    waterfall         LIMITED  (170 of 700 documents are not fully decomposable)
    margin_analytics  LIMITED  (document-grain steps cannot be attributed below the document)
    segmentation      READY
    elasticity        READY
    optimisation      LIMITED  (acceptance is estimated from won deals only)
```

A blocked deliverable caps the band at LIMITED however good the score is. A
dataset can have immaculate fields and still be unable to segment a customer it
never records.

`scripts/run_readiness.py` exits 1 on BLOCKED, so it can gate a pipeline.

---

## 4. Margin analytics (D3)

Four views, in `pricing/analytics/margin.py`. Each abstains where the grain does
not support it.

**Leakage bridge** — of the 8.48M given away between list and pocket, 43.5% is
negotiated off-list, 43.3% on-invoice discount, 13.2% off-invoice leakage.
`leakage_bridge(w, "product")` returns the bars it can and marks the rest
incomplete, because a bridge missing one bar is worse than no bridge.

**Price band** — the spread of realised unit price for the same product. The
widest is PRD-020 at a 10.3% interquartile spread over a 28.3% full range. This
uses the *realised* price, never list; banding list price returns a spread of
zero and calls it a finding, which is the bug I wrote first.

**Margin quadrants** — volume against margin rate, split at the book's own
medians rather than a round number imported from someone else's business.

**Margin at risk** — 106 of 530 certified transactions earn below a 10% margin
floor, carrying 6.34M of pocket price (28.0% of the book). Lifting them to the
floor is worth 847,917.

---

## 5. The dairy mapping

`axiom-source-mapping (2) (1).json` describes
`Integrated_Dairy_Model_20000_Cows_Al_Kharj_English.xlsx` — 86 triplesMaps, 40
classes, 26 sheets. The workbook itself has not arrived.

Rather than wait, `scripts/make_mapping_fixture.py` runs the mapping backwards
and generates a workbook with exactly the structure it declares: every sheet,
every column the graph depends on, foreign keys that resolve, and money that adds
up. `pricing/mapping/` then reads it back through the same `Source` interface the
CSV snapshot uses.

The onboarding finding, which the fixture makes concrete:

> **The mapping exposes money on `24_Financial_Plan` only.** `23_Invoices` maps
> `InvoiceLine`, `ShipmentLine`, `SalesOrderLine`, `Invoice_ID`, `Customer_ID`,
> `SKU_ID` and `Region_ID` — every identifier and every link, and not one amount.
> There is no list price anywhere: `08_Products` exposes `SKU_ID` and nothing
> else.

So the dairy waterfall runs at plan grain — scenario × SKU × region — with a
five-step chain:

```
  Planned revenue               1,585,278.62   (100.0%)
  - freight and returns            94,745.88   (  6.0%)
  = Net delivered revenue       1,490,532.74   ( 94.0%)
  - product cost                  966,955.70   ( 61.0%)
  = Contribution margin           523,577.04   ( 33.0%)
```

and reconciles against the sheet's own `Planned_Contribution_Margin_SAR` to
2e-11. The four canonical steps the mapping cannot support are absent from the
chain, and the contract reports them UNAVAILABLE with the reason attached:

```json
"list_value": {
  "amount": null,
  "status": "unavailable",
  "note": "no list price is mapped on any sheet; 08_Products exposes SKU_ID only"
}
```

Readiness on it: 80.3/100, **LIMITED**. Waterfall READY; segmentation,
elasticity and optimisation all BLOCKED, each with its reason. That is the
correct answer, and it is the answer I would rather give in week four than in
week seven.

**What to ask the upstream team for**, in priority order:

1. An amount on `23_Invoices` at `InvoiceLine` grain — net line amount, quantity
   and unit price. This alone lifts the waterfall from plan grain to line grain
   and unlocks segmentation and optimisation.
2. A list or reference price on `08_Products`, which is what makes a *waterfall*
   a waterfall rather than a margin bridge.
3. A date on `24_Financial_Plan`. The mapping declares a `Period` class there
   with no columns, so nothing can be sliced by time.

---

## 6. Into Week 5

Segmentation (D4) and elasticity (D5), both against `b2b_industrial` — the dairy
fixture cannot carry them and says so. Write-up in
[`WEEK5_SEGMENTATION_AND_ELASTICITY.md`](WEEK5_SEGMENTATION_AND_ELASTICITY.md).
