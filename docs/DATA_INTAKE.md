# Data intake — what the engine reads, and what happens when you swap it

Owner: Nourjihen Elabed · engine v0.4.0

Short answer to the question this document exists for: **yes, the engine reads
CSVs out of `data/`, and no, the mapping does not describe them.** Those are two
different schemas and it matters which one you are handing it.

---

## 1. What is actually read

Three backends implement one interface, `graph.source.Source` — named tables in,
provenance out. Which one runs is `PRICING_SOURCE`:

| `PRICING_SOURCE` | class | reads |
|---|---|---|
| `csv` (default) | `CsvGraphSource` | a directory of CSVs |
| `workbook` | `WorkbookSource` | an `.xlsx` through an R2RML-style mapping |
| `neo4j` | `Neo4jGraphSource` | the live graph over Bolt, read-only |

The CSV backend resolves *file names from the active profile*, not from a
constant:

```python
CsvGraphSource.path_for("transactions")   # -> data/snapshot_.../transactions.csv
```

`config.CSV_DIR` picks the newest `data/snapshot_*` directory by name, or
whatever `PRICING_DATA_DIR` says. It does **not** check that the directory suits
the profile — an auto-pick that silently corrects itself is how a wrong number
becomes hard to trace. `scripts/check_data.py` is the thing that checks.

---

## 2. The two schemas are not the same schema

This is the part worth being precise about.

**`b2b_industrial`** — eight CSVs, the synthetic distributor snapshot:

```
transactions.csv  transaction_lines.csv  transaction_discounts.csv  discounts.csv
customers.csv     products.csv           channels.csv               regions.csv
```

**`dairy_al_kharj`** — thirteen logical tables, named after the *mapping's*
sheets:

```
01_Assumptions  02_Data_Dictionary  03_Calendar  04_Regions  08_Products
09_Customers    19_Sales_Orders     20_Shipments 22_Returns  23_Invoices
24_Financial_Plan  25_Reconciliations  26_Dashboard
```

Dropping the dairy data into `data/` and running the default profile will not
work, and should not: nothing in one schema means anything in the other. What
you change is the profile, not the folder:

```bash
PRICING_PROFILE=dairy_al_kharj python scripts/run_waterfall.py
```

### The dairy profile reads CSVs too

`Profile.file_for()` turns a bare sheet name into a file name, so a folder of
CSVs named after the sheets is a valid source for a workbook profile:

```
data/snapshot_dairy_2026-09/
  24_Financial_Plan.csv
  23_Invoices.csv
  08_Products.csv
  ...
```

```bash
PRICING_PROFILE=dairy_al_kharj PRICING_DATA_DIR=data/snapshot_dairy_2026-09 \
  python scripts/run_waterfall.py
```

So when the real dairy data arrives you can hand it over as either the workbook
or a folder of CSVs, and nothing in the engine changes.

### A graph is not a folder, and does not become one

The AXIOM knowledge graph arrives as one JSON document — every node with its
labels and properties, every relationship as a source/target pair. It is read as
it is:

```bash
PRICING_PROFILE=axiom_poc PRICING_REPORTS_DIR=reports_axiom_poc \
  python scripts/run_waterfall.py
```

No conversion step, no snapshot directory, no `PRICING_SOURCE`. `graph_json` is a
backend beside `csv`, `neo4j` and `workbook`: it answers `table("orders")` like
the others, and the profile cannot tell which one it is holding. The profile
declares `default_source="graph_json"` because that is where its data natively
lives, and `get_source` honours the declaration unless someone asks for something
else out loud.

That last point is the whole reason to do it this way. In production this module
sits on top of a live graph — there is no import step to hang anything on, and a
pipeline whose first stage is "convert to CSV" is a pipeline that cannot run
against Bolt without being rewritten. Reading the export in place means the shape
of the code is already the production shape, and swapping to Neo4j is one
environment variable rather than a design change.

**A table is a node label.** The profile's table map holds whatever the source
natively calls a thing, which in a graph is the label:

```python
tables={"orders": "Order", "order_lines": "OrderLine", ...}
```

The `graph_json` and `neo4j` backends look up the label. A directory-backed
source looks for `Order.csv`. One map, three backends.

**Integrity runs at the entry gate, not at import.** This export states most
foreign keys twice — once as an edge, once as a denormalised property, so
`OrderLine.order_id` sits beside `(:OrderLine)-[:PART_OF]->(:Order)`. The profile
reads the property because it is cheaper, which means a disagreement between the
two would be a wrong number with a clean-looking audit trail. All 7,402 of them
are checked on every read, along with the export's own node and edge counts,
whether every edge lands on a node that is present, and whether every business
key is unique. Any of these failing is BLOCKING — the engine refuses rather than
reporting a number computed on a graph that contradicts itself.

`PRICING_REPORTS_DIR` matters here. `reports/` is not keyed by profile, so two
clients write `waterfall_totals.csv` to the same name and the second run silently
replaces the first one's numbers with a different book's.

### Freezing a snapshot, and why you still would

```bash
python scripts/import_graph_json.py --verify
dvc add data/snapshot_axiom_poc_v1
```

Not to read it — to pin it. A DVC-tracked directory is a content hash you can put
beside an MLflow run and come back to in six months. The export itself is a file
that whoever sends the next one will replace in place, and then an old report can
no longer be reproduced.

The snapshot is one CSV per node label, read by the same profile through the
`csv` backend, and it must produce the same totals to the cent. That is asserted
rather than assumed:

```bash
pytest tests/test_axiom_poc.py -k graph_and_frozen_snapshot
```

---

## 3. Before you run anything, check it

```bash
python scripts/check_data.py                          # the configured snapshot
python scripts/check_data.py data/snapshot_2026-09-01 # a folder you just dropped in
python scripts/check_data.py --identify data/incoming # which profile is this?
python scripts/check_data.py --describe               # what must the data look like?
```

Exit code 1 when the data would not run, so it gates a pipeline or a CI job.

### The three ways a hand-off breaks, and what each looks like

**A missing file** — loud on its own, but the check names what it was for:

```
Conformance: UNUSABLE (87.5% of tables complete) -- b2b_industrial
  Missing tables:
    - channels               expected channels.csv
```

**A renamed column** — the dangerous one. Nothing raises. pandas returns the
frame, the merge produces NaN, and the engine faithfully reports a smaller book
that looks entirely reasonable:

```
  Missing columns:
    - products: unit_cost
```

**A number exported as text** — quieter still, because it *runs*. `"59,440.02"`
parses to NaN, so the column reads as blank and every total silently
under-reports:

```
Conformance: USABLE (100.0% of tables complete)
  Present but would not parse as numbers or dates:
    - transactions: gross_amount
```

USABLE is the important middle verdict: the run will complete, and the run is
wrong. That is why the checker reads the file *before* the loader coerces it —
after `_normalise` has run, an unparseable price is already NaN and
indistinguishable from a genuinely empty cell.

### What the check covers, and what it cannot

Declared columns are checked directly, from `Profile.semantic_types`. Columns the
profile's *extraction* reaches for — `customer_id`, `product_id`, the join keys —
are not declared anywhere, so the check closes that gap by **running the
extraction**: a column it needs and cannot find raises, and the error comes back
as `engine_error`. Between the two, everything that matters is covered.

---

## 4. Sending a spec upstream

```bash
python scripts/check_data.py --describe --profile dairy_al_kharj
```

gives one row per file: the file name, the workbook sheet it corresponds to, and
which columns must parse as numbers and as dates. That table is the thing to
attach to the email.

---

## 5. Adding a schema nobody has seen before

1. Write the mapping, or accept the client's.
2. Add a `Profile` in `profiles.py`: the step chain, the dimensions, the table
   map, the semantic types, and one extraction function.
3. `python scripts/check_data.py --profile <name>` until it says EXACT.
4. `python scripts/run_readiness.py --profile <name>` to see which deliverables
   the data can carry.

No engine file is touched at any point. That is the test of whether the
generalisation actually worked, and it is why the dairy profile exists — a second
schema is the only way to find the places where the first one was hardcoded.
