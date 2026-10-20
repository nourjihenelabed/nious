# Snapshots

`snapshot_synthetic_v1/` is DVC-tracked (see `.dvc` files and `data/.gitignore`).
It is an input we receive, not something the pipeline produces.

`snapshot_dairy_tiny_v1/` is different: it is *generated* from
`axiom-source-mapping*.json` by `scripts/make_mapping_fixture.py`, deterministically
and in under a second. It is small enough to commit, and committing it means the
mapping tests run without regenerating anything. Regenerate it whenever the client
sends a new mapping:

```bash
python scripts/make_mapping_fixture.py
```

It is a structural stand-in, not a forecast. Every value in it is a placeholder;
what is real is the set of sheets, the set of columns, and which keys resolve.

`snapshot_axiom_poc_v1/` is a *frozen copy* of the AXIOM knowledge graph, one CSV
per node label. The engine does not read it -- `PRICING_PROFILE=axiom_poc` reads
the JSON export in place, which is the shape it runs in against a live graph.
This exists so a result can be cited: a DVC content hash beside an MLflow run,
returnable to in six months, unlike an export file that gets replaced in place.

```bash
python scripts/import_graph_json.py --verify
dvc add data/snapshot_axiom_poc_v1
```

It must produce the same totals as the graph to the cent; `tests/test_axiom_poc.py`
asserts it.
