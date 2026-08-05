# Week 1 — The data as I understand it

**AXIOM Pricing Module · Track A (Pricing Engine & MLOps)** · Nourjihen Elabed
Snapshot: synthetic CSV export, sha256 `6af98ff70daf16a1…` · 700 transactions, 2025-01-01 → 2026-06-30

---

## Caveat on the source

Neo4j credentials have not been issued, so this profile is of the **synthetic CSV
snapshot**, not the live graph. The graph-access layer is written against both, and the
Cypher for the real graph is already in `src/pricing/cypher.py`; `scripts/export_neo4j_to_csv.py`
will reproduce this exact CSV shape from Neo4j so nothing downstream changes. **Every
number below must be re-verified against the real graph on day one of access** — I have
written the profiling as code, not prose, precisely so that re-verification is one command.

## What is in the graph

| Table | Rows | Maps to |
|---|---|---|
| `transactions` | 700 | `Transaction` nodes |
| `transaction_lines` | 1,408 | `CONTAINS` edges (qty, unit_price, line_total) |
| `transaction_discounts` | 1,997 | `DISCOUNTED_BY` edges (discount_amount_applied) |
| `customers` | 40 | `Customer` |
| `products` | 24 | `Product` |
| `discounts` | 8 | `Discount` catalogue |
| `channels` | 5 | `Channel` |
| `regions` | 6 | `Region` |

18 months of history, 40 customers evenly split across Enterprise / Mid-Market / SMB /
Public Sector, 5 channels, 6 regions (2 currencies). **Referential integrity is clean** —
zero dangling edges on all 8 checks; every transaction resolves to a customer, a channel
and at least one product line.

The pricing signal lives on the **edges**, not the nodes: quantity and realised unit price
are `CONTAINS` properties, and the actual money given away is `DISCOUNTED_BY.discount_amount_applied`.
The `Discount` node only carries a headline `rate`.

## Finding 1 — `net_amount` does not mean what it usually means

The Data Contract lists `gross_amount` and `net_amount` without defining them. Testing
four hypotheses against the data (`profiling.amount_reconciliation`):

| Hypothesis | Holds? | Max abs error |
|---|---|---|
| `gross_amount` = Σ line_total | **yes** | 6e-11 |
| `net_amount` = gross − **on**-invoice discounts | no | €58,316 |
| `net_amount` = gross − all discounts | no | €63,161 |
| `net_amount` = gross − **off**-invoice discounts | **yes** | 3e-11 |

So `net_amount` nets off the *off-invoice* leakage and leaves the on-invoice discounts in —
the reverse of the standard reading, where the invoice/net price is what appears on the
invoice, i.e. after on-invoice discounts and before off-invoice leakage.

**How I have handled it:** the engine computes every waterfall step from the `DISCOUNTED_BY`
edges and `Discount.type`, which is unambiguous. `net_amount` is carried through for
reconciliation only and is never a waterfall step. A test pins the observation so we notice
immediately if the real graph differs.

**→ Question for the Week 2 gate:** is this the intended semantics, or a generator artefact?

## Finding 2 — small null counts, wide blast radius

| Column | Nulls | Why it matters |
|---|---|---|
| `discounts.amount` | 8 / 8 (100%) | Catalogue is entirely rate-based. Not a defect — but confirm the real graph is too, since a mixed rate/amount catalogue needs different handling. |
| `products.unit_cost` | 2 / 24 | Breaks COGS → **no pocket margin** for any transaction touching PRD-007 or PRD-018 |
| `products.list_price` | 1 / 24 | Breaks the top of the waterfall → **no price realisation** for PRD-013 |
| `transaction_discounts.discount_amount_applied` | 11 / 1,997 | Discount applied, size unknown → leakage understated |
| `transactions.payment_terms` | 21 / 700 | Segmentation feature (Week 5), not a waterfall input |
| `customers.industry` / `tenure_years` | 2 / 1 | Segmentation features |

Three null product fields propagate to **170 of 700 transactions (24%)** that cannot be
fully decomposed. This is the leverage point of the whole snapshot: 3 missing cells on
reference data invalidate a quarter of the book.

**How I have handled it:** a transaction missing any input reports that step as
UNAVAILABLE (NaN), never 0 and — importantly — never a *partial sum*. My first
implementation summed the available lines, which produced plausible-looking but wrong list
values (one transaction showed list value *below* invoiced value, i.e. a negative
discount). A partially-known total is not an estimate; it is unavailable. Headline totals
therefore cover only the 530 certified transactions, with coverage stated next to them,
and the 170 excluded ones stay visible in the flags rather than being deleted.

## Finding 3 — the leakage is real and it is where the Brief said it would be

Certified book (530 transactions):

```
  List value                   31,108,764      100.0%
  - negotiated off-list         3,688,235       11.9%   ← never invoiced at list
  = Gross (invoiced)           27,420,529       88.1%
  - on-invoice discounts        3,669,610       11.8%   ← volume / contract / promo
  = Invoice price              23,750,919       76.3%
  - off-invoice leakage         1,117,996        3.6%   ← rebates, freight, terms, co-op, side letters
  = Pocket price               22,632,923       72.8%
  - COGS                       18,513,092       59.5%
  = Pocket margin               4,119,831       13.2%

  Pocket margin %  18.20%      Price realisation  72.75%
```

27% of list value never reaches the pocket — consistent with the 15–20% leakage the Brief
describes, and larger once the negotiated off-list gap is included.

All eight discount types are in use. Off-invoice types individually touch 26–31% of
transactions and collectively reach **79% of the book** — the "small concessions that
accumulate silently" in the Brief, and the reason on- and off-invoice must never be netted
into one number.

One caution: the applied amounts do **not** equal catalogue `rate` × gross. Realised rates
vary widely per deal (e.g. DSC-ON-CTR is a 12% headline but ranges 0.01–26.8% in practice).
The catalogue rate is a label; the edge amount is the truth. Always read the edge.

## Finding 4 — the data is thin for what Week 5 asks of it

700 transactions across 40 customers: median **18 deals per customer**, minimum 10. Split
across 4 segments × 6 regions × 5 channels, per-cell counts fall into single digits fast.

This is not a problem to fix, it is a constraint to design around, and it confirms the
onboarding plan's insistence on **hierarchical shrinkage and credible intervals** for
elasticity rather than per-segment regressions. I expect several segments to produce
intervals crossing zero, and per the plan those must be flagged as unfit to drive
optimisation rather than used anyway.

## Open questions for the Week 2 architecture review

1. **`net_amount` semantics** (Finding 1) — intended, or artefact?
2. **Channel `cost_to_serve` and `commission_rate` are so far unused.** Do they belong
   inside pocket margin, or below it as cost-to-serve? This is a definition to declare, not
   to infer — and it changes every margin number. My default until told otherwise: exclude
   from pocket margin, report separately.
3. **Currency.** Regions carry a currency but transactions carry no FX rate. Are all amounts
   already in a common currency, or is conversion assumed?
4. **Is the "negotiated off-list" gap (11.9%) a real waterfall step** we should surface, or
   an artefact of list prices being maintained loosely? No line is priced *above* list
   (0/1,408), which suggests list is a genuine ceiling and the gap is real discounting that
   happens before any `Discount` node is involved.
5. **Missing reference data** — is it worth asking the upstream team to complete the 3 null
   product fields? Three cells would move coverage from 76% to 100%.

## What exists in code

`src/pricing/` — `config` (backend switch), `cypher` (all queries, read-only by
construction and tested as such), `data_source` (CSV active / Neo4j ready),
`profiling` (the 10 profiles above), `gross_to_net` (Week 1 waterfall cut).
`scripts/` — `profile_data.py`, `quick_win_gross_to_net.py`, `export_neo4j_to_csv.py`.
`notebooks/01_graph_profiling.ipynb` · `tests/test_week1.py` (10 tests, all passing,
including the waterfall against an independent hand computation at 0.01% tolerance).
