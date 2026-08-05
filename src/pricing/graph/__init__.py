"""
Graph-access layer (D1).

Read-only access to the AXIOM knowledge graph, plus the entry-point gate that
decides whether a snapshot is fit to compute on at all.

    source.py      GraphSource: CsvGraphSource (active) | Neo4jGraphSource (ready)
    cypher.py      every query, read-only by construction
    validation.py  refuse-to-run gate (Brief 7.4)
"""

from .source import CsvGraphSource, GraphSource, Neo4jGraphSource, get_source
from .validation import (
    Finding,
    Severity,
    SnapshotRejected,
    ValidationReport,
    require_valid,
    validate,
)

__all__ = [
    "GraphSource", "CsvGraphSource", "Neo4jGraphSource", "get_source",
    "validate", "require_valid", "ValidationReport", "Finding", "Severity",
    "SnapshotRejected",
]
