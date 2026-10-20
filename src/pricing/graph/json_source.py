"""Reading the AXIOM knowledge graph from its own export, with no CSV in between."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .. import config
from .source import GraphSource, _normalise
from .validation import Finding, Severity

#: Business keys that must be unique, or every join downstream fans out silently.
BUSINESS_KEYS: tuple[tuple[str, str], ...] = (
    ("Order", "order_id"),
    ("OrderLine", "order_line_id"),
    ("SellableProduct", "product_id"),
    ("TradeCustomer", "customer_id"),
    ("PriceComponent", "component_id"),
    ("Opportunity", "opportunity_id"),
    ("Quote", "quote_id"),
    ("Channel", "channel_id"),
    ("SalesAgent", "agent_id"),
    ("RivalSeller", "competitor_id"),
)

#: The edge/property pairs that state the same fact twice.
CROSS_CHECKS: tuple[tuple[str, str, str, str, str], ...] = (
    ("OrderLine", "PART_OF", "Order", "order_id", "order_id"),
    ("OrderLine", "FOR_PRODUCT", "SellableProduct", "product_id", "product_id"),
    ("Order", "FULFILLS", "Opportunity", "opportunity_id", "opportunity_id"),
    ("Opportunity", "FOR_CUSTOMER", "TradeCustomer", "customer_id", "customer_id"),
    ("Opportunity", "SOLD_THROUGH", "Channel", "channel_id", "channel_id"),
    ("Quote", "FOR_OPPORTUNITY", "Opportunity", "opportunity_id", "opportunity_id"),
    ("Quote", "ISSUED_BY", "SalesAgent", "issued_by_agent_id", "agent_id"),
    ("DealOutcome", "FOR_OPPORTUNITY", "Opportunity", "opportunity_id", "opportunity_id"),
    ("StageChange", "FOR_OPPORTUNITY", "Opportunity", "opportunity_id", "opportunity_id"),
    ("CompetitorPresence", "FOR_OPPORTUNITY", "Opportunity", "opportunity_id", "opportunity_id"),
    ("CompetitorPresence", "OF_COMPETITOR", "RivalSeller", "competitor_id", "competitor_id"),
    ("CustomerPeriod", "FOR_CUSTOMER", "TradeCustomer", "customer_id", "customer_id"),
    ("CustomerClassPeriod", "FOR_CUSTOMER", "TradeCustomer", "customer_id", "customer_id"),
    ("ListPricePeriod", "FOR_PRODUCT", "SellableProduct", "product_id", "product_id"),
    ("ProductCostPeriod", "FOR_PRODUCT", "SellableProduct", "product_id", "product_id"),
    ("ProductStagePeriod", "FOR_PRODUCT", "SellableProduct", "product_id", "product_id"),
    ("ProductSubstitute", "FOR_PRODUCT", "SellableProduct", "product_id", "product_id"),
    ("AuthorityPeriod", "FOR_AGENT", "SalesAgent", "agent_id", "agent_id"),
    ("IncentivePeriod", "FOR_AGENT", "SalesAgent", "agent_id", "agent_id"),
    ("PriceComponent", "APPLIES_TO_LINE", "OrderLine", "applies_to_id", "order_line_id"),
    ("PriceComponent", "APPLIES_TO_ORDER", "Order", "applies_to_id", "order_id"),
)

UNLABELLED = "_unlabelled"


class NotAGraphExport(ValueError):
    """Raised when a JSON file is not one of these exports."""


#: Names the relationship list goes by at the top level of a single-document export.
RELATIONSHIP_KEYS: tuple[str, ...] = ("relationships", "rels", "edges", "links")

#: File stems looked for when an export arrives as a folder of line-delimited files.
NODE_STEMS: tuple[str, ...] = ("nodes",)
RELATIONSHIP_STEMS: tuple[str, ...] = ("rels", "relationships", "edges")
LINE_SUFFIXES: tuple[str, ...] = (".ndjson", ".jsonl", ".json")


def read_export(path: Path) -> dict:
    """Load an export in any of the layouts AXIOM ships, refusing anything else."""
    if path.is_dir():
        return _read_folder(path)
    text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise NotAGraphExport(f"{path} is not readable JSON: {exc}") from exc
        # More than one JSON value in the file: one record per line.
        doc = _split_records(_read_lines(path, text), path)
        if not doc["relationships"]:
            # Half of a folder export, most likely: reading it alone would serve
            # every table while every edge check passed vacuously.
            raise NotAGraphExport(
                f"{path} holds nodes but no relationships. If it sits beside "
                f"rels.ndjson, point at the folder instead: {path.parent}"
            ) from None
    if isinstance(doc, list):
        doc = _split_records(doc, path)
    rel_key = next((k for k in RELATIONSHIP_KEYS if isinstance(doc, dict) and k in doc), None)
    if not isinstance(doc, dict) or "nodes" not in doc or rel_key is None:
        found = sorted(doc)[:8] if isinstance(doc, dict) else type(doc).__name__
        raise NotAGraphExport(
            f"{path} does not look like an AXIOM graph export: expected 'nodes' and "
            f"'relationships' at the top level, found {found}"
        )
    if rel_key != "relationships":
        doc["relationships"] = doc.pop(rel_key)
    return doc


def is_export_folder(path: Path) -> bool:
    """True when `path` is a folder holding a line-delimited graph export."""
    return path.is_dir() and _find(path, NODE_STEMS) is not None


def export_files(path: Path) -> list[Path]:
    """The files an export was read from, in a fixed order, for hashing and sizing."""
    if not path.is_dir():
        return [path]
    found = [_find(path, NODE_STEMS), _find(path, RELATIONSHIP_STEMS), path / "meta.json"]
    return [p for p in found if p is not None and p.exists()]


def _find(folder: Path, stems: tuple[str, ...]) -> Path | None:
    """The first `<stem><suffix>` file present in `folder`."""
    for stem in stems:
        for suffix in LINE_SUFFIXES:
            if (folder / f"{stem}{suffix}").is_file():
                return folder / f"{stem}{suffix}"
    return None


def _read_lines(path: Path, text: str | None = None) -> list:
    """Every JSON value in a file, whether it holds one document or one per line."""
    text = path.read_text(encoding="utf-8") if text is None else text
    try:
        whole = json.loads(text)
        return whole if isinstance(whole, list) else [whole]
    except json.JSONDecodeError:
        pass
    records = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise NotAGraphExport(
                f"{path} line {number} is not readable JSON: {exc}"
            ) from exc
    return records


def _split_records(records: list, path: Path) -> dict:
    """Sort a flat list of records into nodes and relationships by their keys."""
    nodes, rels = [], []
    for record in records:
        if isinstance(record, dict) and "labels" in record and "id" in record:
            nodes.append(record)
        elif isinstance(record, dict) and {"source", "target", "type"} <= record.keys():
            rels.append(record)
        else:
            shown = sorted(record)[:6] if isinstance(record, dict) else type(record).__name__
            raise NotAGraphExport(
                f"{path} does not look like an AXIOM graph export: a record is "
                f"neither a node nor a relationship (keys {shown})"
            )
    return {"nodes": nodes, "relationships": rels}


def _read_folder(folder: Path) -> dict:
    """Assemble one document from `nodes.ndjson`, `rels.ndjson` and `meta.json`."""
    nodes_file = _find(folder, NODE_STEMS)
    rels_file = _find(folder, RELATIONSHIP_STEMS)
    if nodes_file is None or rels_file is None:
        present = sorted(p.name for p in folder.iterdir())[:8]
        raise NotAGraphExport(
            f"{folder} does not look like an AXIOM graph export folder: expected "
            f"nodes.ndjson and rels.ndjson, found {present}"
        )
    doc = {
        "nodes": _split_records(_read_lines(nodes_file), nodes_file)["nodes"],
        "relationships": _split_records(_read_lines(rels_file), rels_file)["relationships"],
    }
    meta_file = folder / "meta.json"
    if meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        doc["exported_at"] = meta.get("exported_at")
        doc["session_id"] = meta.get("session_id")
        # The folder layout states its counts in `upstream_meta`, or as bare
        # integers under `nodes` / `relationships`; both mean the same thing.
        summary = meta.get("summary") or meta.get("upstream_meta") or {}
        if not summary and isinstance(meta.get("nodes"), int):
            summary = {
                "node_count": meta["nodes"],
                "relationship_count": meta.get("relationships"),
            }
        doc["summary"] = summary
    return doc


class GraphJsonSource(GraphSource):
    """A `Source` over an AXIOM graph JSON export, read in memory."""

    name = "graph_json"

    def __init__(self, export: Path | str | None = None, profile=None):
        """Open an export, failing loudly if it is missing or is not one."""
        from ..profiles import get_profile

        self.profile = profile or get_profile()
        self.export_path = Path(export or config.GRAPH_JSON)
        if not self.export_path.exists():
            raise FileNotFoundError(
                f"Graph export not found: {self.export_path}\n"
                "Set PRICING_GRAPH_JSON in .env, or pass --json on the command line."
            )
        self.document = read_export(self.export_path)
        self._frames: dict[str, pd.DataFrame] | None = None
        self._cache: dict[str, pd.DataFrame] = {}

    # -- the graph itself ---------------------------------------------------

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        """One DataFrame per node label, built on first use."""
        if self._frames is None:
            rows: dict[str, list[dict]] = defaultdict(list)
            for node in self.document["nodes"]:
                label = (node.get("labels") or [UNLABELLED])[0]
                rows[label].append({"_node_id": node["id"], **(node.get("properties") or {})})
            self._frames = {
                label: pd.DataFrame(
                    records, columns=list(dict.fromkeys(k for r in records for k in r))
                )
                for label, records in rows.items()
            }
        return self._frames

    def labels(self) -> dict[str, int]:
        """Every node label in the export, with its count."""
        return dict(
            Counter((n.get("labels") or [UNLABELLED])[0] for n in self.document["nodes"])
        )

    def relationships(self) -> pd.DataFrame:
        """The edge list, with each endpoint's label resolved."""
        node_label = self._node_label()
        return pd.DataFrame(
            [
                {
                    "source_id": r["source"],
                    "source_label": node_label.get(r["source"], ""),
                    "type": r["type"],
                    "target_id": r["target"],
                    "target_label": node_label.get(r["target"], ""),
                }
                for r in self.document["relationships"]
            ],
            columns=["source_id", "source_label", "type", "target_id", "target_label"],
        )

    def _node_label(self) -> dict[str, str]:
        """Node id -> its first label."""
        return {
            n["id"]: (n.get("labels") or [UNLABELLED])[0] for n in self.document["nodes"]
        }

    # -- the Source interface ----------------------------------------------

    def table_names(self) -> tuple[str, ...]:
        """The logical tables this profile expects the graph to carry."""
        return tuple(self.profile.tables)

    def label_for(self, logical: str) -> str:
        """The node label a logical table maps to."""
        return str(self.profile.tables.get(logical, logical))

    def raw_table(self, name: str) -> pd.DataFrame:
        """One logical table with every value as text."""
        df = self._label_frame(name)
        return df.astype(str).replace({"None": "", "nan": "", "NaT": ""})

    def table(self, name: str) -> pd.DataFrame:
        """One logical table, read once and normalised to the profile's types."""
        if name not in self.profile.tables:
            raise KeyError(
                f"Unknown table {name!r} for profile {self.profile.name!r}. "
                f"Known: {sorted(self.profile.tables)}"
            )
        if name not in self._cache:
            self._cache[name] = _normalise(name, self._label_frame(name), self.profile)
        return self._cache[name].copy()

    def _label_frame(self, name: str) -> pd.DataFrame:
        """The frame for one logical table, or a FileNotFoundError naming the label."""
        label = self.label_for(name)
        if label not in self.frames:
            raise FileNotFoundError(
                f"The graph carries no {label!r} nodes, which profile "
                f"{self.profile.name!r} reads as its {name!r} table. "
                f"Labels present: {sorted(self.frames)}"
            )
        return self.frames[label].copy()

    def snapshot_info(self) -> dict:
        """Backend, location, read time, and a sha256 over the export's bytes."""
        return {
            "backend": self.name,
            "location": str(self.export_path),
            "profile": self.profile.name,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "exported_at": self.document.get("exported_at"),
            "session_id": self.document.get("session_id"),
            "content_sha256": self._content_sha256(),
            "node_counts": self.labels(),
        }

    def _content_sha256(self) -> str:
        """sha256 over the export's bytes; a folder hashes its files in a fixed order."""
        digest = hashlib.sha256()
        for part in export_files(self.export_path):
            digest.update(part.read_bytes())
        return digest.hexdigest()

    # -- integrity ----------------------------------------------------------

    def graph_integrity(self) -> list[Finding]:
        """Check the export against itself, in order of how badly a "no" would hurt."""
        findings: list[Finding] = []
        nodes, edges = self.document["nodes"], self.document["relationships"]
        summary = self.document.get("summary") or {}

        stated_nodes = summary.get("node_count")
        stated_edges = summary.get("relationship_count")
        if stated_nodes is not None and stated_nodes != len(nodes):
            findings.append(
                Finding(
                    Severity.BLOCKING, "export completeness",
                    f"the export states {stated_nodes:,} nodes but carries {len(nodes):,}; "
                    "it was truncated in transit",
                    abs(stated_nodes - len(nodes)),
                )
            )
        if stated_edges is not None and stated_edges != len(edges):
            findings.append(
                Finding(
                    Severity.BLOCKING, "export completeness",
                    f"the export states {stated_edges:,} relationships but carries "
                    f"{len(edges):,}; it was truncated in transit",
                    abs(stated_edges - len(edges)),
                )
            )
        if summary.get("complete") is False:
            findings.append(
                Finding(
                    Severity.BLOCKING, "export completeness",
                    "the export declares itself incomplete",
                )
            )

        node_label = self._node_label()
        dangling = sum(
            1 for r in edges if r["source"] not in node_label or r["target"] not in node_label
        )
        if dangling:
            findings.append(
                Finding(
                    Severity.BLOCKING, "dangling edge",
                    "relationships point at nodes that are not in the export", dangling,
                )
            )

        for label, key in BUSINESS_KEYS:
            frame = self.frames.get(label)
            if frame is None or key not in frame.columns:
                continue
            n_dup = int(frame[key].duplicated().sum())
            if n_dup:
                findings.append(
                    Finding(
                        Severity.BLOCKING, "duplicate business key",
                        f"{label}.{key} is not unique, so every join through it fans out",
                        n_dup,
                    )
                )

        findings.extend(self._key_agreement(node_label))
        return findings

    def _key_agreement(self, node_label: dict[str, str]) -> list[Finding]:
        """Do the denormalised foreign keys agree with the edges beside them?"""
        by_type: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for r in self.document["relationships"]:
            by_type[(node_label.get(r["source"], ""), r["type"])].append(
                (r["source"], r["target"])
            )
        props = {n["id"]: (n.get("properties") or {}) for n in self.document["nodes"]}

        findings = []
        for src_label, edge, tgt_label, src_prop, tgt_prop in CROSS_CHECKS:
            pairs = [
                (s, t) for s, t in by_type.get((src_label, edge), [])
                if node_label.get(t) == tgt_label
            ]
            disagree = sum(
                1 for s, t in pairs
                if str(props[s].get(src_prop)) != str(props[t].get(tgt_prop))
            )
            if disagree:
                findings.append(
                    Finding(
                        Severity.BLOCKING, "contradictory key",
                        f"{src_label}.{src_prop} disagrees with "
                        f"-[:{edge}]->{tgt_label}.{tgt_prop} on {disagree} of "
                        f"{len(pairs)} edges; the profile reads the property, so "
                        "these would be wrong numbers with a clean audit trail",
                        disagree,
                    )
                )
        return findings

    def integrity_frame(self) -> pd.DataFrame:
        """The integrity checks as a table, for a report rather than a gate."""
        findings = self.graph_integrity()
        if not findings:
            return pd.DataFrame(
                [{"check": "graph integrity", "status": "ok", "detail": "", "n_affected": 0}]
            )
        return pd.DataFrame(
            [
                {
                    "check": f.check,
                    "status": f.severity.value,
                    "detail": f.detail,
                    "n_affected": f.n_affected,
                }
                for f in findings
            ]
        )
